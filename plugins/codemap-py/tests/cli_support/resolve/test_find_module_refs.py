"""Tests for ``bin/find_module_refs.py`` — residual-reference search for ``rename-refs``.

The parity tests run ``grep -rn`` command lines against the same fixture tree and require identical hits, so the pattern
translation (BRE -> ``re``) is proven, not asserted. The oracles are the skill's pre-extraction greps, except that the
``import`` and ``from-import`` oracles accept leading blanks (``^[[:blank:]]*``): an indented import inside a function,
``try`` or ``if TYPE_CHECKING:`` block is a residual reference the old line-start anchor missed.

The parity tests keep the real ``grep`` call because that oracle is the contract; everything else is pure in-process
file walking under ``tmp_path``. Output is compared as line lists, never through Markdown or prose patterns.
"""

from __future__ import annotations

import json
import ntpath
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import find_module_refs
import pytest
from codemap_py import scanner

_MODULE = "pkg.old"
_NAME = "old_name"

#: Fixture tree: ``relative path -> text``. Decoys (``pkgXold``, ``pkg.older``) prove literal dots and word boundaries.
_TREE = {
    "pkg/__init__.py": 'from .old import old_name\nfrom . import other\n__all__ = ["old_name", \'other\', "unrelated"]\n',
    "pkg/old.py": "def old_name():\n    return 1\n",
    "pkg/consumer.py": (
        "import pkg.old\n"
        "import pkg.old as po\n"
        "import pkg.old.sub\n"
        "import pkg.older\n"
        "import pkg.old_thing\n"
        "import pkgXold\n"
        "from pkg.old import old_name\n"
        "from pkg.old as y\n"
        "from pkg.oldish import other\n"
        "    import pkg.old  # indented: a function-body import is a residual too\n"
        'note = "see import pkg.old"  # mid-line: not an import statement\n'
        "        from pkg.old import old_name  # indented from-import\n"
    ),
    "pkg/sub/__init__.py": "from .old import thing\nfrom pkg.sub.old import z\nfrom sub.old import w\nfrom .older import q\n",
    "docs/api.rst": ":func:`pkg.old.old_name`\n:mod:`pkg.old`\n:class:`Other`\n:mod:`pkg.older`\n",
    "docs/conf.py": '"""See :attr:`x.old_name.y` and :meth:`old_name`."""\n',
    "pyproject.toml": 'packages = ["pkg.old"]\nname = "pkgXold"\n',
    "setup.cfg": "install_requires =\n    pkg.old\n",
    "nested/pyproject.toml": 'packages = ["pkg.old"]\n',
    ".venv/lib/vendored.py": "import pkg.old\n",
    "node_modules/pkg/__init__.py": 'from .old import x\n__all__ = ["old_name"]\n',
}


def _symlinks_are_available() -> bool:
    """Return whether this host can create and resolve a file symlink."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("target\n", encoding="utf-8")
        try:
            link.symlink_to(target)
        except OSError:
            return False
        return link.is_symlink() and link.read_text(encoding="utf-8") == "target\n"


_skip_symlinks_unavailable = pytest.mark.skipif(
    not _symlinks_are_available(), reason="file symlink creation is unavailable on this host"
)
_skip_grep_unavailable = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("grep") is None, reason="needs a POSIX grep for the parity oracle"
)


def _write_tree(root: Path, tree: dict[str, str]) -> None:
    """Write each ``relative path -> text`` entry of *tree* under *root*, creating parent directories."""
    for relative, text in tree.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write ``_TREE`` under ``tmp_path`` and make it the working directory (the skill runs from the repo root)."""
    _write_tree(tmp_path, _TREE)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_with_err(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[list[str], str]:
    """Run ``main`` and return the printed hit lines plus everything written to stderr."""
    assert find_module_refs.main(list(argv)) == 0
    captured = capsys.readouterr()
    return captured.out.splitlines(), captured.err


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> list[str]:
    """Run ``main`` and return the printed hit lines."""
    return _run_with_err(capsys, *argv)[0]


def _grep(pattern: str, scope: list[str], includes: tuple[str, ...] = ()) -> list[str]:
    """Run the pre-extraction ``grep -rn`` (pruned dirs excluded, as the helper does); return sorted hit lines."""
    skips = [f"--exclude-dir={name}" for name in sorted(find_module_refs._SKIP_DIRS)]
    flags = [f"--include={glob}" for glob in includes]
    done = subprocess.run(
        ["grep", "-rn", *skips, *flags, "-e", pattern, *scope],
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted(done.stdout.splitlines())


# ---------------------------------------------------------------------------
# parity with the pre-extraction grep command lines
# ---------------------------------------------------------------------------

_BT = "`"
#: Pre-extraction BRE patterns, verbatim from the old skill (dots escaped by its sed idiom).
_ORIGINAL_GREPS = [
    pytest.param(
        "all-exports", ["--name", _NAME], f"\"{_NAME}\"\\|'{_NAME}'", ["."], ("__init__.py",), id="all-exports"
    ),
    pytest.param(
        "sphinx-symbol",
        ["--name", _NAME],
        "\\|".join(f":{role}:{_BT}[^']*{_NAME}[^']*{_BT}" for role in ("func", "class", "meth", "mod", "attr")),
        ["."],
        ("*.py", "*.rst"),
        id="sphinx-symbol",
    ),
    pytest.param(
        "import",
        ["--module", _MODULE],
        "^[[:blank:]]*import pkg\\.old\\b\\|^[[:blank:]]*import pkg\\.old as ",
        ["."],
        ("*.py",),
        id="import",
    ),
    pytest.param(
        "from-import",
        ["--module", _MODULE],
        "^[[:blank:]]*from pkg\\.old import\\|^[[:blank:]]*from pkg\\.old as ",
        ["."],
        ("*.py",),
        id="from-import",
    ),
    pytest.param(
        "relative",
        ["--module", _MODULE],
        "from \\.*[^.]*\\.old import\\|from \\.old import\\|from \\.old as ",
        ["pkg"],
        ("__init__.py",),
        id="relative",
    ),
    pytest.param("config", ["--module", _MODULE], "pkg\\.old", ["pyproject.toml", "setup.cfg"], (), id="config"),
    pytest.param(
        "sphinx-mod",
        ["--module", _MODULE],
        f":mod:{_BT}[^']*pkg\\.old[^']*{_BT}",
        ["."],
        ("*.py", "*.rst"),
        id="sphinx-mod",
    ),
]


class TestParityWithGrep:
    """Each kind prints exactly what the old inline ``grep -rn`` printed."""

    @_skip_grep_unavailable
    @pytest.mark.parametrize(("kind", "inputs", "pattern", "scope", "includes"), _ORIGINAL_GREPS)
    def test_hits_match_original_grep(
        self,
        repo: Path,
        capsys: pytest.CaptureFixture[str],
        kind: str,
        inputs: list[str],
        pattern: str,
        scope: list[str],
        includes: tuple[str, ...],
    ) -> None:
        """The helper prints the same hit lines as the original ``grep -rn`` command for each search kind.

        The fixture tree has decoys for dot-any-char, word boundary, indentation, and pruned dirs, so a wrong BRE
        translation shows up as a differing hit set. The grep runs for real: it is the oracle being proven against.
        """
        expected = _grep(pattern, scope, includes)

        actual = sorted(_run(capsys, "--kind", kind, *inputs))

        assert expected, "fixture must give the oracle something to find"
        assert actual == expected


# ---------------------------------------------------------------------------
# per-kind behaviour
# ---------------------------------------------------------------------------


class TestKinds:
    """What each kind does and does not report."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            # Only __init__.py files are searched, and a double- or single-quoted name hits.
            pytest.param(
                ["--kind", "all-exports", "--name", "other"],
                ['./pkg/__init__.py:3:__all__ = ["old_name", \'other\', "unrelated"]'],
                id="all-exports-either-quote-style-init-files-only",
            ),
            # --scope replaces the default "." and paths are printed relative to it as given.
            pytest.param(
                ["--kind", "all-exports", "--name", "old_name", "--scope", "pkg"],
                ['pkg/__init__.py:3:__all__ = ["old_name", \'other\', "unrelated"]'],
                id="all-exports-scope-narrows-to-package-dir",
            ),
            # pkg.older, pkg.old_thing, pkgXold and mid-line text are decoys; an indented import is a residual.
            pytest.param(
                ["--kind", "import", "--module", _MODULE],
                [
                    "./pkg/consumer.py:1:import pkg.old",
                    "./pkg/consumer.py:2:import pkg.old as po",
                    "./pkg/consumer.py:3:import pkg.old.sub",
                    "./pkg/consumer.py:10:    import pkg.old  # indented: a function-body import is a residual too",
                ],
                id="import-word-boundary-and-statement-start",
            ),
            # from pkg.oldish import is a different module and stays out; an indented from-import is reported.
            pytest.param(
                ["--kind", "from-import", "--module", _MODULE],
                [
                    "./pkg/consumer.py:7:from pkg.old import old_name",
                    "./pkg/consumer.py:8:from pkg.old as y",
                    "./pkg/consumer.py:12:        from pkg.old import old_name  # indented from-import",
                ],
                id="from-import-excludes-longer-module-name",
            ),
            # Default scope is "pkg" (module minus last component): sub/__init__.py is found, "from .older" is not.
            pytest.param(
                ["--kind", "relative", "--module", _MODULE],
                [
                    "pkg/__init__.py:1:from .old import old_name",
                    "pkg/sub/__init__.py:1:from .old import thing",
                    "pkg/sub/__init__.py:3:from sub.old import w",
                ],
                id="relative-defaults-to-package-dir-and-init-files",
            ),
            # A top-level module maps to a directory named after itself; absent, so no hits and no error.
            pytest.param(["--kind", "relative", "--module", "old"], [], id="relative-missing-default-scope"),
            # nested/pyproject.toml is not searched; the dot in the module is literal (pkgXold is not a hit).
            pytest.param(
                ["--kind", "config", "--module", _MODULE],
                ['pyproject.toml:1:packages = ["pkg.old"]', "setup.cfg:2:    pkg.old"],
                id="config-reads-only-two-packaging-files",
            ),
            # --scope nested reads nested/pyproject.toml and tolerates the missing setup.cfg.
            pytest.param(
                ["--kind", "config", "--module", _MODULE, "--scope", "nested"],
                ['nested/pyproject.toml:1:packages = ["pkg.old"]'],
                id="config-scope-points-at-another-directory",
            ),
            # :func:/:attr:/:meth: roles that contain the bare name are reported from both .rst and .py files.
            pytest.param(
                ["--kind", "sphinx-symbol", "--name", _NAME],
                [
                    "./docs/api.rst:1::func:`pkg.old.old_name`",
                    './docs/conf.py:1:"""See :attr:`x.old_name.y` and :meth:`old_name`."""',
                ],
                id="sphinx-symbol-every-role-in-py-and-rst",
            ),
        ],
    )
    def test_reports_exact_hits(
        self, repo: Path, capsys: pytest.CaptureFixture[str], argv: list[str], expected: list[str]
    ):
        """Each search kind reports exactly the residual lines for its documented scope and nothing else.

        The shared fixture tree mixes real references with decoys (longer names, other paths, mid-line text, pruned
        dirs); the expected list for each kind and scope is the whole contract, including the empty answer.
        """
        assert _run(capsys, *argv) == expected

    def test_attribute_reports_dotted_uses_outside_import_lines(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``pkg.old.func()`` after ``import pkg`` is reported; longer names, other paths and import lines are not.

        An attribute use carries no import line naming the module, so the import kinds cannot see it.
        """
        (repo / "pkg" / "user.py").write_text(
            "import pkg\nimport pkg.old.sub\n"
            "value = pkg.old.old_name()\nother = pkg.older.x\npath = my.pkg.old.y\nbare = pkg.old\n",
            encoding="utf-8",
        )

        hits = _run(capsys, "--kind", "attribute", "--module", _MODULE, "--scope", "pkg/user.py")

        assert hits == ["pkg/user.py:3:value = pkg.old.old_name()"]

    def test_sphinx_mod_prefers_full_path(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The full dotted path matches as a substring of the role (as before), and stderr stays quiet.

        ``pkg.old`` and the longer ``pkg.older`` both contain the path, so both roles are hits and no fallback note is
        printed.
        """
        hits, err = _run_with_err(capsys, "--kind", "sphinx-mod", "--module", _MODULE)

        assert hits == ["./docs/api.rst:2::mod:`pkg.old`", "./docs/api.rst:4::mod:`pkg.older`"]
        assert err == ""

    def test_sphinx_mod_falls_back_to_basename_and_says_so(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No full-path match -> basename-only hits, flagged on stderr so the caller verifies package context.

        Only ``:mod:`other.old``` exists, so searching ``missing.old`` falls back to the basename. The note goes to
        stderr, never into the hit lines the caller parses.
        """
        (repo / "docs" / "api.rst").write_text(":mod:`other.old`\n", encoding="utf-8")

        hits, err = _run_with_err(capsys, "--kind", "sphinx-mod", "--module", "missing.old")

        assert hits == ["./docs/api.rst:1::mod:`other.old`"]
        assert "basename-only" in err

    def test_role_split_across_lines_is_not_hit(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The whole-file pre-scan may match across a newline; only a same-line match is reported, like ``grep``.

        A role broken over two lines in ``split.rst`` must not appear, because ``grep`` matches line by line.
        """
        (repo / "docs" / "split.rst").write_text(":func:`pkg.\nold_name`\n", encoding="utf-8")

        hits = _run(capsys, "--kind", "sphinx-symbol", "--name", _NAME)

        assert not [line for line in hits if "split.rst" in line]

    def test_hits_sorted_by_path_then_line(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Walk order is deterministic: directories and files sorted, line numbers ascending.

        Output order must not depend on filesystem enumeration order, or reports would differ between machines.
        """
        hits = _run(capsys, "--kind", "from-import", "--module", _MODULE)

        assert hits == sorted(hits, key=lambda line: (line.split(":")[0], int(line.split(":")[1])))


# ---------------------------------------------------------------------------
# walk rules
# ---------------------------------------------------------------------------


class TestWalk:
    """Which files are visited."""

    @mock.patch("find_module_refs.os.walk", autospec=True)
    def test_windows_walk_paths_use_posix_separators(
        self, mock_walk: mock.Mock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Native Windows walk paths keep the leading dot and reach consumers with ``/`` separators.

        ``os.walk`` on Windows joins with ``\\``; the grep pipeline this script replaced printed ``./pkg/x.py``, which
        every consumer parses. The scope prefix stays verbatim, as ``grep -r`` prints it.
        """
        monkeypatch.setattr(find_module_refs.os, "sep", ntpath.sep)
        mock_walk.return_value = iter([(ntpath.join(".", "pkg"), [], ["a.py"])])

        assert list(find_module_refs.iter_files(".", ())) == ["./pkg/a.py"]

    def test_vcs_venv_and_cache_dirs_not_entered(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``.venv`` and ``node_modules`` hold third-party code that a rename never targets.

        Both pruned directories contain a matching ``import pkg.old``; neither may appear in the hits.
        """
        hits = _run(capsys, "--kind", "import", "--module", _MODULE)

        assert not [line for line in hits if ".venv" in line or "node_modules" in line]

    def test_skipped_dirs_subset_of_index_exclusions(self) -> None:
        """The search never skips a directory the index does not also skip.

        A directory pruned here but indexed elsewhere would hide references the index still reports.
        """
        assert find_module_refs._SKIP_DIRS <= scanner.SKIP_DIRS

    @_skip_symlinks_unavailable
    def test_symlinked_files_not_followed(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``grep -r`` skips symlinks met while recursing; so does the walk.

        A symlink to ``consumer.py`` would double every hit if it were followed.
        """
        (repo / "pkg" / "alias.py").symlink_to(repo / "pkg" / "consumer.py")

        hits = _run(capsys, "--kind", "import", "--module", _MODULE)

        assert not [line for line in hits if "alias.py" in line]

    def test_file_scope_searched_regardless_of_include_filter(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--scope`` may name one file; a ``.txt`` is searched by an ``import`` kind that filters on ``*.py``.

        Naming a file explicitly overrides the extension filter, as ``grep -r --include`` does for command-line files.
        """
        (repo / "notes.txt").write_text("import pkg.old\n", encoding="utf-8")

        hits = _run(capsys, "--kind", "import", "--module", _MODULE, "--scope", "notes.txt")

        assert hits == ["notes.txt:1:import pkg.old"]

    def test_missing_scope_yields_nothing_and_succeeds(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A scope that does not exist is "no hits", not an error (the old greps discarded stderr).

        Skills pass a derived directory that may not exist for a top-level module.
        """
        assert _run(capsys, "--kind", "import", "--module", _MODULE, "--scope", "no/such/dir") == []

    def test_undecodable_bytes_do_not_abort_search(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A latin-1 file beside the matches is read with replacement characters, never a traceback.

        The byte ``0xe9`` is invalid UTF-8; the match on the next line must still be found.
        """
        (repo / "pkg" / "legacy.py").write_bytes(b"# calf\xe9\nimport pkg.old\n")

        hits = _run(capsys, "--kind", "import", "--module", _MODULE)

        assert "./pkg/legacy.py:2:import pkg.old" in hits

    def test_crlf_line_endings_stripped(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A Windows-style file prints clean lines and still matches ``^import`` anchors.

        A trailing ``\\r`` would both pollute the printed hit and break end-anchored matching.
        """
        (repo / "pkg" / "win.py").write_bytes(b"import pkg.old\r\n")

        hits = _run(capsys, "--kind", "import", "--module", _MODULE, "--scope", "pkg/win.py")

        assert hits == ["pkg/win.py:1:import pkg.old"]


# ---------------------------------------------------------------------------
# defaults and argument errors
# ---------------------------------------------------------------------------


class TestArguments:
    """Scope derivation and usage errors."""

    @pytest.mark.parametrize(
        ("module", "expected"),
        [
            pytest.param("pkg.sub.old", "pkg/sub", id="nested"),
            pytest.param("pkg.old", "pkg", id="one-level"),
            pytest.param("old", "old", id="top-level-keeps-bash-suffix-strip-semantics"),
            pytest.param("", ".", id="empty"),
        ],
    )
    def test_default_relative_scope(self, module: str, expected: str) -> None:
        """Module minus its last component with dots turned into slashes; ``.`` when empty.

        A top-level module keeps its own name, mirroring the Bash suffix-strip the old inline code used.
        """
        assert find_module_refs.default_relative_scope(module) == expected

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--kind", "all-exports"], id="all-exports-needs-name"),
            pytest.param(["--kind", "sphinx-symbol", "--module", "pkg.old"], id="sphinx-symbol-needs-name"),
            pytest.param(["--kind", "import"], id="import-needs-module"),
            pytest.param(["--kind", "relative", "--name", "x"], id="relative-needs-module"),
            pytest.param(["--kind", "unknown", "--module", "m"], id="unknown-kind"),
            pytest.param(["--module", "m"], id="kind-missing"),
        ],
    )
    def test_missing_or_unknown_input_is_usage_error(self, argv: list[str]) -> None:
        """Argparse exits 2 for a kind without its required value, an unknown kind, or no kind.

        Each kind needs either ``--name`` or ``--module``; running without it would search for an empty pattern.
        """
        with pytest.raises(SystemExit) as raised:
            find_module_refs.main(argv)

        assert raised.value.code == 2

    def test_regex_metacharacters_in_input_literal(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A name full of regex syntax matches only itself, never as a pattern.

        Searching ``a.b`` must not hit ``axb``, and a lone ``(x`` must not raise a regex error.
        """
        (repo / "pkg" / "__init__.py").write_text('__all__ = ["a.b", "axb", "(x"]\n', encoding="utf-8")

        hits = _run(capsys, "--kind", "all-exports", "--name", "a.b", "--scope", "pkg")

        assert hits == ['pkg/__init__.py:1:__all__ = ["a.b", "axb", "(x"]']


# ---------------------------------------------------------------------------
# ``name`` kind, ``--files-from-json`` and ``--alias-in`` (rename-refs non-call references)
# ---------------------------------------------------------------------------

#: ``Base`` is defined in ``pkg/sub/a.py`` and re-exported by ``pkg/__init__.py``. Only ``d.py`` and ``a.py`` call it;
#: ``c.py`` subclasses it, tests it with ``isinstance`` and registers a callback, ``e.py`` subclasses the re-export.
_REEXPORT_TREE = {
    "pkg/__init__.py": 'from .sub.a import Base, lonely\n\n__all__ = ["Base", "lonely"]\n',
    "pkg/sub/__init__.py": "",
    "pkg/sub/a.py": "class Base:\n    pass\n\n\ndef lonely():\n    return 2\n\n\ndef target():\n    return Base()\n",
    "pkg/c.py": (
        "from pkg.sub.a import Base, lonely\n\n\nclass Child(Base):\n    pass\n\n\n"
        "HOOKS = [lonely]\n\n\ndef check(x):\n    return isinstance(x, Base)\n"
    ),
    "pkg/d.py": "from pkg import Base\n\n\ndef make():\n    return Base()\n",
    "pkg/e.py": "from pkg import Base\n\n\nclass E(Base):\n    pass\n",
    "other/f.py": "class BaseModel:\n    pass\n",
}
_MODULES = ["pkg", "pkg.c", "pkg.d", "pkg.e", "pkg.sub", "pkg.sub.a"]


@pytest.fixture
def reexport_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write ``_REEXPORT_TREE`` under ``tmp_path`` and make it the working directory."""
    _write_tree(tmp_path, _REEXPORT_TREE)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _listing(names: list[str]) -> dict[str, object]:
    """Build a complete ``list --limit 0`` answer for ``names`` (packages map to ``__init__.py``)."""
    packages = {"pkg", "pkg.sub"}
    modules = [{"name": n, "path": n.replace(".", "/") + ("/__init__.py" if n in packages else ".py")} for n in names]
    return {"modules": modules, "total": len(modules), "shown": len(modules)}


def _save(root: Path, filename: str, payload: object) -> str:
    """Write ``payload`` as JSON under ``root`` and return the file name (relative to the working directory)."""
    (root / filename).write_text(json.dumps(payload), encoding="utf-8")
    return filename


def _engine_answers(root: Path) -> list[str]:
    """Save the engine-shaped ``fn-rdeps``, narrowed ``find-symbol`` and ``batch`` answers; return their CLI args."""
    fn_rdeps = {
        "qname": "pkg.sub.a::Base",
        "called_by": [
            {"caller": "pkg.d::make", "module": "pkg.d", "path": "pkg/d.py"},
            {"caller": "pkg.sub.a::target", "module": "pkg.sub.a", "path": "pkg/sub/a.py"},
        ],
        "count": 2,
    }
    find_symbol = {"matches": [{"qualified_name": "Base", "module": "pkg.sub.a", "path": "pkg/sub/a.py"}]}
    batch = {
        "batch": [
            {"ok": True, "index": 0, "cmd": "list", "result": _listing(_MODULES)},
            {
                "ok": True,
                "index": 1,
                "cmd": "rdeps",
                "result": {"module": "pkg.sub.a", "imported_by": ["pkg", "pkg.c"]},
            },
            {"ok": False, "index": 2, "cmd": "rdeps", "error": "not indexed", "result": {"error": "not indexed"}},
            {"ok": True, "index": 3, "cmd": "rdeps", "result": {"module": "pkg", "imported_by": ["pkg.d", "pkg.e"]}},
        ]
    }
    names = (("fn-rdeps.json", fn_rdeps), ("find-symbol.json", find_symbol), ("batch.json", batch))
    return [arg for filename, payload in names for arg in ("--files-from-json", _save(root, filename, payload))]


class TestNameKind:
    """Whole-word references a call graph never records, and the file set ``rename-refs`` edits them in."""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("class Child(Base):", id="subclass-base"),
            pytest.param("ok = isinstance(x, Base)", id="isinstance-target"),
            pytest.param("HOOKS = [Base]", id="callback-value"),
            pytest.param('__all__ = ["Base"]', id="all-export-string"),
            pytest.param("from pkg import Base", id="import-line"),
            pytest.param("x = pkg.Base()", id="attribute-access"),
            pytest.param("def f(x: Base) -> None: ...", id="annotation"),
        ],
    )
    def test_non_call_references_are_hits(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str) -> None:
        """Every non-call spelling of the name is reported with its line.

        Subclassing, ``isinstance``, callbacks, ``__all__`` strings, imports, attributes and annotations never show up
        in a call graph, so only this text search can find them.
        """
        (tmp_path / "m.py").write_text(text + "\n", encoding="utf-8")

        assert _run(capsys, "--kind", "name", "--name", "Base", "--scope", str(tmp_path / "m.py")) == [
            f"{tmp_path / 'm.py'}:1:{text}"
        ]

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("class BaseModel: ...", id="longer-suffix"),
            pytest.param("class MyBase: ...", id="longer-prefix"),
            pytest.param("_Base = 1", id="underscore-prefix"),
            pytest.param("Base2 = 1", id="digit-suffix"),
        ],
    )
    def test_longer_identifiers_not_hits(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str) -> None:
        """A name that merely contains the old one (``old`` -> ``old_v2`` style) is not a residual.

        The match is whole-word: a longer prefix or suffix, an underscore, or a digit makes it a different identifier.
        """
        (tmp_path / "m.py").write_text(text + "\n", encoding="utf-8")

        assert _run(capsys, "--kind", "name", "--name", "Base", "--scope", str(tmp_path / "m.py")) == []

    def test_pyi_stubs_searched_and_other_files_not(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Type stubs carry references too; prose files are left to the Sphinx kinds.

        A directory holding ``m.pyi``, ``notes.txt`` and ``doc.rst`` each containing the name must report only the stub.
        """
        _write_tree(tmp_path, {"m.pyi": "Base\n", "notes.txt": "Base\n", "doc.rst": "Base\n"})

        # grep -r style: the scope prints as given, the walked part joins with "/" on every OS
        assert _run(capsys, "--kind", "name", "--name", "Base", "--scope", str(tmp_path)) == [
            f"{tmp_path}/m.pyi:1:Base"
        ]

    def test_reference_sites_cover_importers_and_reexport_users(
        self, reexport_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Callers, the module's importers and a re-exporting package's importers are all listed.

        The engine answers (``fn-rdeps``, narrowed ``find-symbol``, ``batch``) name five files; non-call uses such as
        subclasses in ``c.py`` and ``e.py`` must be found there.
        """
        hits = _run(capsys, "--kind", "name", "--name", "Base", *_engine_answers(reexport_repo))

        assert sorted({hit.split(":", 1)[0] for hit in hits}) == [
            "pkg/__init__.py",
            "pkg/c.py",
            "pkg/d.py",
            "pkg/e.py",
            "pkg/sub/a.py",
        ]
        assert "pkg/c.py:4:class Child(Base):" in hits
        assert "pkg/e.py:4:class E(Base):" in hits

    def test_call_edges_alone_miss_non_call_files(
        self, reexport_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ``fn-rdeps`` answer by itself names only the calling files — the gap the batch closes.

        Without the importer answers, ``c.py`` and ``e.py`` (subclass, ``isinstance``) would stay unedited by a rename.
        """
        answers = _engine_answers(reexport_repo)

        hits = _run(capsys, "--kind", "name", "--name", "Base", *answers[:2])

        assert sorted({hit.split(":", 1)[0] for hit in hits}) == ["pkg/d.py", "pkg/sub/a.py"]

    def test_files_from_json_ignores_scope(self, reexport_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The answers decide the files; ``other/`` stays unsearched even when ``--scope`` points there.

        ``other/f.py`` defines ``BaseModel``; with ``--files-from-json`` the scope must not widen the file set.
        """
        hits = _run(capsys, "--kind", "name", "--name", "Base", "--scope", "other", *_engine_answers(reexport_repo))

        assert not any(hit.startswith("other/") for hit in hits)

    @pytest.mark.parametrize(
        "stub",
        [
            pytest.param("class Base: ...", id="class"),
            pytest.param("def Base(*args, **kwargs): ...", id="function"),
            pytest.param("async def Base(*args, **kwargs): ...", id="async-function"),
            pytest.param("    def Base(*args, **kwargs): ...", id="indented-method"),
        ],
    )
    def test_alias_in_skips_stub_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], stub: str) -> None:
        """The ``--deprecate`` stub line named by ``--alias-in`` is not a residual; other uses still are.

        The module holds a compatibility stub (class, function, async function or indented method) plus a real use of
        the name; only the real use may be reported.
        """
        module = tmp_path / "m.py"
        module.write_text(f"{stub}\nuse = Base\n", encoding="utf-8")

        hits = _run(capsys, "--kind", "name", "--name", "Base", "--scope", str(module), "--alias-in", str(module))

        assert hits == [f"{module}:2:use = Base"]

    @pytest.mark.parametrize(
        "stub",
        [
            pytest.param("class Base: ...", id="class"),
            pytest.param("def Base(*args, **kwargs): ...", id="function"),
            pytest.param("async def Base(*args, **kwargs): ...", id="async-function"),
            pytest.param("    def Base(*args, **kwargs): ...", id="indented-method"),
        ],
    )
    def test_surviving_definition_is_residual_without_alias_in(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], stub: str
    ) -> None:
        """Without ``--alias-in`` a surviving definition of the old name is reported like any other reference.

        Same module as the alias case, searched without the exemption: the definition line counts as a residual.
        """
        module = tmp_path / "m.py"
        module.write_text(f"{stub}\nuse = Base\n", encoding="utf-8")

        hits = _run(capsys, "--kind", "name", "--name", "Base", "--scope", str(module))

        assert hits == [f"{module}:1:{stub}", f"{module}:2:use = Base"]

    def test_alias_in_limited_to_its_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A same-name definition in another file still counts.

        Two files define ``Base``; exempting ``a.py`` must leave ``b.py``'s definition reported.
        """
        (tmp_path / "a.py").write_text("class Base: ...\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("class Base: ...\n", encoding="utf-8")

        hits = _run(
            capsys, "--kind", "name", "--name", "Base", "--scope", str(tmp_path), "--alias-in", str(tmp_path / "a.py")
        )

        # grep -r style: the scope prints as given, the walked part joins with "/" on every OS
        assert hits == [f"{tmp_path}/b.py:1:class Base: ..."]

    def test_alias_in_needs_name_kind(self) -> None:
        """``--alias-in`` with another kind is a usage error, not a silent no-op.

        An exemption that silently did nothing would let a caller believe a stub had been skipped.
        """
        with pytest.raises(SystemExit) as raised:
            find_module_refs.main(["--kind", "all-exports", "--name", "x", "--alias-in", "m.py"])

        assert raised.value.code == 2

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"error": "Symbol not found"}, id="error-answer"),
            pytest.param([1, 2], id="not-an-object"),
            pytest.param(
                {"batch": [{"cmd": "rdeps", "ok": True, "result": {"module": "pkg"}}]}, id="batch-without-list"
            ),
            pytest.param(
                {"batch": [{"cmd": "list", "ok": True, "result": {"modules": [], "total": 3, "shown": 0}}]},
                id="truncated-list",
            ),
            pytest.param(
                {
                    "batch": [
                        {"cmd": "list", "ok": True, "result": _listing(_MODULES)},
                        {"cmd": "rdeps", "ok": True, "result": {"module": "pkg", "index": {"truncated": True}}},
                    ]
                },
                id="truncated-rdeps",
            ),
        ],
    )
    def test_unusable_answer_exits_1_and_prints_no_sites(
        self, reexport_repo: Path, capsys: pytest.CaptureFixture[str], payload: object
    ) -> None:
        """A site list built from a broken answer would silently skip importers, so the search refuses to run.

        Error answers, non-objects, malformed batches and truncated listings all exit 1 with nothing on stdout.
        """
        source = _save(reexport_repo, "answer.json", payload)

        code = find_module_refs.main(["--kind", "name", "--name", "Base", "--files-from-json", source])

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "reference sites unknown" in captured.err

    def test_missing_answer_file_exits_1(self, reexport_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An answer file that was never written stops the search instead of searching nothing.

        The earlier query step may have failed before writing; the helper must say so, not report zero references.
        """
        code = find_module_refs.main(["--kind", "name", "--name", "Base", "--files-from-json", "absent.json"])

        captured = capsys.readouterr()
        assert code == 1
        assert "cannot read query answer absent.json" in captured.err
