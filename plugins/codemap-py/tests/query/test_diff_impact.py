"""Diff-impact: git diff → changed modules + symbols → blast radius in one JSON.

The subcommand joins per-module ``rdeps``/``coupled``, per-symbol ``fn-rdeps``, and a union ``test-impact`` for every
module the working-tree diff touches, tiered by reverse-dependency count. Tests drive it over a real git fixture repo
because the diff source is ``git diff --name-only``; the real ``git`` and ``scan-*`` subprocesses are the contract, so
none of them is mocked.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _scan_helpers import index_path_for, run_git, run_query, run_scan

#: Source of ``lib.py`` with ``helper`` returning ``x + {offset}``; ``untouched`` never changes.
_LIB_TEMPLATE = "def helper(x):\n    return x + {offset}\n\n\ndef untouched(y):\n    return y\n"


def _diff_impact(scan_query: Path, root: Path, index_path: Path, *args: str) -> dict:
    """Run ``diff-impact`` (without self-heal) with extra *args* and return the parsed JSON."""
    return run_query(scan_query, root, index_path, "--no-heal", "diff-impact", *args)


def _commit(root: Path, message: str, *extra: str) -> None:
    """Commit the index of *root* under a throwaway identity, passing *extra* options to ``git commit``."""
    run_git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", *extra, "-m", message)


def _init_committed_repo(root: Path) -> None:
    """Initialise *root* as a git repository holding one ``base`` commit of everything in it."""
    run_git(root, "init", "-q")
    run_git(root, "add", ".")
    _commit(root, "base")


def _review_diff(root: Path) -> bytes:
    """Return the working-tree diff of *root* the way a review's ``diff.patch`` is taken (three context lines)."""
    return subprocess.run(
        ["git", "diff", "--unified=3", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"],
        cwd=str(root),
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture(name="git_project")
def _git_project(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create an indexed repository for diff-impact tests."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lib.py").write_text(_LIB_TEMPLATE.format(offset=1))
    (root / "app.py").write_text("import lib\n\n\ndef run(x):\n    return lib.helper(x)\n")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_lib.py").write_text("import lib\n\n\ndef test_helper():\n    assert lib.helper(1) == 2\n")
    _init_committed_repo(root)
    run_scan(scan_index, root)
    index_path = index_path_for(root)
    assert index_path.exists()
    return root, index_path


@pytest.fixture(name="edited_project")
def _edited_project(git_project: tuple[Path, Path]) -> tuple[Path, Path]:
    """Return ``git_project`` after one uncommitted edit of ``lib.helper`` (``x + 1`` becomes ``x + 2``)."""
    root, index_path = git_project
    (root / "lib.py").write_text(_LIB_TEMPLATE.format(offset=2))
    return root, index_path


class TestDiffImpact:
    """Working-tree changes map to modules, symbols, risk tiers, and test impact."""

    def test_clean_tree_reports_nothing(self, git_project, scan_query):
        """No diff → zero changed files, empty module list, LOW overall risk.

        A freshly committed and indexed repository has nothing to report, which must not read as a risky change.
        """
        root, index_path = git_project

        data = _diff_impact(scan_query, root, index_path)

        assert data["changed_files"] == 0
        assert data["changed_modules"] == []
        assert data["highest_risk"] == "LOW"

    def test_changed_symbol_and_risk_tier(self, edited_project, scan_query):
        """Editing one function surfaces exactly that symbol, its importers, and a tier.

        ``lib`` is imported by ``app`` and ``test_lib``, which puts the change in the MODERATE blast-radius tier.
        """
        root, index_path = edited_project

        data = _diff_impact(scan_query, root, index_path)

        assert data["changed_files"] == 1
        (entry,) = data["changed_modules"]
        assert entry["module"] == "lib"
        assert entry["changed_symbols"] == ["lib::helper"]
        # lib is imported by app and test_lib → 1–4 importers = MODERATE blast radius
        assert entry["risk"] == "MODERATE"
        assert data["highest_risk"] == "MODERATE"

    def test_test_impact_union_names_test_file(self, edited_project, scan_query):
        """The union test-impact block points at the test exercising the changed module."""
        root, index_path = edited_project

        data = _diff_impact(scan_query, root, index_path)

        assert any("test_lib.py" in f for f in data["test_impact"]["test_files"])
        assert "pytest" in data["test_impact"]["pytest_cmd"]

    def test_base_ref_scopes_the_diff(self, edited_project, scan_query):
        """``--base <ref>`` diffs against that ref: a committed change is visible via ``HEAD~1``.

        After committing the edit the working tree is clean, so only the ranged query can still see the change.
        """
        root, index_path = edited_project
        _commit(root, "tweak", "-a")

        clean = _diff_impact(scan_query, root, index_path)
        ranged = _diff_impact(scan_query, root, index_path, "--base", "HEAD~1")

        assert clean["changed_files"] == 0
        assert [m["module"] for m in ranged["changed_modules"]] == ["lib"]

    def test_unindexed_changed_file_reported_unmapped(self, git_project, scan_query):
        """A tracked change to a file the index does not know is surfaced, never hidden."""
        root, index_path = git_project
        (root / "extra.py").write_text("def brand_new():\n    return 0\n")
        run_git(root, "add", "extra.py")

        data = _diff_impact(scan_query, root, index_path)

        assert "extra.py" in data["unmapped_files"]

    def test_output_is_deterministic(self, edited_project, scan_query):
        """Two identical runs produce identical impact payloads (coverage block aside)."""
        root, index_path = edited_project
        first = _diff_impact(scan_query, root, index_path)
        second = _diff_impact(scan_query, root, index_path)

        first.pop("index", None)
        second.pop("index", None)

        assert first == second

    def test_single_coverage_block(self, edited_project, scan_query):
        """Diff-impact emits exactly one coverage block for the whole join."""
        root, index_path = edited_project

        data = _diff_impact(scan_query, root, index_path)

        assert "index" in data
        assert all("index" not in m for m in data["changed_modules"])


class TestDiffFileMode:
    """``--diff-file`` feeds the change set from a fetched unified diff (PR-review mode)."""

    _DIFF = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def helper(x):\n"
        "-    return x + 1\n"
        "+    return x + 9\n"
    )

    def test_maps_symbols_without_local_git_change(self, git_project, scan_query):
        """A clean working tree still reports the diff-file's change set, symbol-mapped.

        The diff file describes a change the local checkout never made, as when reviewing someone else's pull request.
        """
        root, index_path = git_project
        diff_path = root / "pr.diff"
        diff_path.write_text(self._DIFF)

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        assert data["base"] == f"diff-file:{diff_path}"
        (entry,) = data["changed_modules"]
        assert entry["module"] == "lib"
        assert entry["changed_symbols"] == ["lib::helper"]

    def test_ignores_non_python_and_deleted_files(self, git_project, scan_query):
        """Doc files and /dev/null (deletions) contribute nothing to the change set."""
        root, index_path = git_project
        diff_path = root / "pr.diff"
        diff_path.write_text("+++ b/README.md\n@@ -1 +1 @@\n+++ /dev/null\n@@ -1,5 +0,0 @@\n")

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        assert data["changed_files"] == 0
        assert data["changed_modules"] == []

    @pytest.mark.parametrize(
        ("hunk", "expected"),
        [
            pytest.param(
                "@@ -3,4 +3,4 @@\n \x0c\n \n def untouched(y):\n-    return y\n+    return y + 0\n",
                ["lib::untouched"],
                id="form-feed-context-line",
            ),
            pytest.param(
                "@@ -4,3 +4,3 @@\n # a\u2028b\n def untouched(y):\n-    return y\n+    return y + 0\n",
                ["lib::untouched"],
                id="line-separator-in-context-line",
            ),
            pytest.param(
                "@@ -1,6 +1,6 @@\n def helper(x):\n-    return x + 1\n+    return 'p\x0cq'\n \n \n"
                " def untouched(y):\n-    return y\n+    return y + 0\n",
                ["lib::helper", "lib::untouched"],
                id="form-feed-in-added-line",
            ),
        ],
    )
    def test_line_break_characters_inside_a_line_keep_the_hunk_in_step(self, git_project, scan_query, hunk, expected):
        """A form feed or Unicode line separator inside one diff line never splits it into two body lines.

        The hunk body is walked by its header counts, so every extra line taken from one physical line shifted later
        post-image numbers or ended the hunk early: the added ``return y + 0`` landed outside ``untouched``, or was
        never read at all, and the changed symbol was missing from the answer. Diffs separate lines with ``\\n`` only.
        """
        root, index_path = git_project
        diff_path = root / "pr.diff"
        diff_path.write_text(
            f"diff --git a/lib.py b/lib.py\n--- a/lib.py\n+++ b/lib.py\n{hunk}", encoding="utf-8", newline="\n"
        )

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        assert [module["changed_symbols"] for module in data["changed_modules"]] == [expected]


#: ``Report.render`` is reached only through an instance (``r.render()``), so its static caller count is zero. Line 6
#: is the one the change touches; ``__init__`` (lines 2-3) and ``Sub`` (line 9) sit inside its three-line context.
_METHOD_UTIL = (
    "class Report:\n"
    "    def __init__(self, x):\n"
    "        self.x = x\n"
    "\n"
    "    def render(self):\n"
    "        return self.x\n"
    "\n"
    "\n"
    "class Sub(Report):\n"
    "    pass\n"
)
#: ``_METHOD_UTIL`` with only the body of ``Report.render`` changed.
_METHOD_UTIL_CHANGED = _METHOD_UTIL.replace("return self.x\n", "return self.x + 0\n")


@pytest.fixture(name="method_project")
def _method_project(tmp_path: Path, scan_index: Path) -> tuple[Path, Path, Path]:
    """Create a clean indexed repository plus a reviewer-style diff that changes one line of ``Report.render``.

    The diff is taken with git's default three context lines, as a review's ``diff.patch`` is, then the working tree is
    restored, so ``--diff-file`` answers against a fresh index.
    """
    root = tmp_path / "methods"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    util = root / "pkg" / "util.py"
    util.write_text(_METHOD_UTIL)
    (root / "pkg" / "core.py").write_text(
        "from pkg.util import Report\n\n\ndef show(x):\n    r = Report(x)\n    return r.render()\n"
    )
    _init_committed_repo(root)
    run_scan(scan_index, root)
    util.write_text(_METHOD_UTIL_CHANGED)
    diff_path = tmp_path / "review.diff"
    diff_path.write_bytes(_review_diff(root))
    run_git(root, "checkout", "--", "pkg/util.py")
    return root, index_path_for(root), diff_path


#: Tests for ``_METHOD_UTIL``: a pytest class whose method reaches ``render`` through an instance, and a plain test.
_METHOD_TESTS = (
    "from pkg.util import Report\n"
    "\n"
    "\n"
    "class TestReport:\n"
    "    def test_render(self):\n"
    "        r = Report(1)\n"
    "        assert r.render() == 1\n"
    "\n"
    "\n"
    "def test_plain():\n"
    "    assert Report(2).x == 2\n"
)


@pytest.fixture(name="collected_test_project")
def _collected_test_project(tmp_path: Path, scan_index: Path) -> tuple[Path, Path, dict[str, Path]]:
    """Create a clean indexed repository plus two reviewer-style diffs: tests only, and tests with ``Report.render``.

    Each diff changes the bodies of ``TestReport.test_render`` and ``test_plain``; the mixed one also changes
    ``Report.render``. The working tree is restored after each, so ``--diff-file`` answers against a fresh index.
    """
    root = tmp_path / "collected"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    util = root / "pkg" / "util.py"
    util.write_text(_METHOD_UTIL)
    tests = root / "tests" / "test_util.py"
    tests.write_text(_METHOD_TESTS)
    _init_committed_repo(root)
    run_scan(scan_index, root)
    diffs: dict[str, Path] = {}
    for name, changes_util in (("tests", False), ("mixed", True)):
        tests.write_text(_METHOD_TESTS.replace("== 1\n", "== 1 + 0\n").replace("== 2\n", "== 2 + 0\n"))
        if changes_util:
            util.write_text(_METHOD_UTIL_CHANGED)
        diffs[name] = tmp_path / f"{name}.diff"
        diffs[name].write_bytes(_review_diff(root))
        run_git(root, "checkout", "--", ".")
    return root, index_path_for(root), diffs


class TestHunkContext:
    """A reviewer's diff maps to the lines it changes, never to the context lines around them.

    Every review feeds ``--diff-file`` a default three-context-line diff. Reading the hunk header's post-image range
    named the neighbouring ``__init__`` and ``Sub`` as changed, and each then took one of a specialist's few follow-up
    queries; git mode, reading a zero-context diff, never did.
    """

    def test_diff_file_and_git_mode_name_the_same_changed_symbols(self, method_project, scan_query):
        """The same one-line change yields identical ``changed_symbols`` from the diff file and from git.

        The working tree gets the change the saved review diff describes, so both modes see the same edit and must name
        only ``Report`` and ``Report.render``, not the context neighbours.
        """
        root, index_path, diff_path = method_project
        (root / "pkg" / "util.py").write_text(_METHOD_UTIL_CHANGED)

        git_mode = _diff_impact(scan_query, root, index_path)
        file_mode = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        assert "@@ -3,7 +3,7 @@" in diff_path.read_text()
        assert [m["changed_symbols"] for m in file_mode["changed_modules"]] == [
            m["changed_symbols"] for m in git_mode["changed_modules"]
        ]
        assert file_mode["changed_modules"][0]["changed_symbols"] == ["pkg.util::Report", "pkg.util::Report.render"]

    def test_deletion_only_hunk_falls_back_to_every_symbol_in_both_modes(self, method_project, scan_query, tmp_path):
        """A file whose only change removes lines names every symbol, from the diff file exactly as from git.

        A removed line has no post-image position, so neither mode can place it; both keep the conservative "every
        symbol potentially changed" fallback rather than the context neighbours the header range used to name.
        """
        root, index_path, _ = method_project
        (root / "pkg" / "util.py").write_text(
            _METHOD_UTIL.replace("        return self.x\n\n\n", "        return self.x\n\n")
        )
        diff_path = tmp_path / "deletion.diff"
        diff_path.write_bytes(_review_diff(root))

        git_mode = _diff_impact(scan_query, root, index_path)
        file_mode = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        assert [m["changed_symbols"] for m in file_mode["changed_modules"]] == [
            m["changed_symbols"] for m in git_mode["changed_modules"]
        ]
        assert file_mode["changed_modules"][0]["changed_symbols"] == [
            "pkg.util::Report",
            "pkg.util::Report.__init__",
            "pkg.util::Report.render",
            "pkg.util::Sub",
        ]


class TestUnresolvedCallers:
    """A changed method with zero static callers is disclosed on diff-impact exactly as ``fn-rdeps`` discloses it.

    ``diff-impact`` is the one structural answer every review carries. It reported ``caller_count: 0`` for a method
    reached only through an instance with no hint and no ``not_covered``, so the change read as touching dead code.
    """

    def test_zero_caller_method_entry_carries_the_fn_rdeps_hint(self, method_project, scan_query):
        """Only the instance-called method's entry carries the hint; the constructed class has a static caller."""
        root, index_path, diff_path = method_project

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        (module,) = data["changed_modules"]
        hinted = {entry["qname"]: entry["hint"].partition(". ")[0] for entry in module["fn_rdeps"] if "hint" in entry}
        assert hinted == {"pkg.util::Report.render": 'Find references with grep -rnE "\\.render\\b"'}

    def test_payload_names_the_unresolved_symbols_and_their_blind_spots(self, method_project, scan_query):
        """A top-level hint names every unresolved symbol and the coverage block lists the call graph's blind spots."""
        root, index_path, diff_path = method_project

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        assert data["hint"].endswith("Unresolved (1): pkg.util::Report.render")
        assert data["index"]["not_covered"] == ["dynamic-dispatch", "hook-callbacks", "string-dispatch"]

    def test_complete_note_defers_to_the_hint(self, method_project, scan_query):
        """A complete hinted answer never tells the reader that verification is not needed."""
        root, index_path, diff_path = method_project

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        assert (data["index"]["query_complete"], "not needed" in data["index"]["note"]) == (True, False)

    def test_test_only_diff_carries_no_hint_and_no_blind_spots(self, collected_test_project, scan_query):
        """A diff that changes only test methods discloses nothing unresolved: the runner, not code, calls them.

        Every changed ``Test*.test_*`` method carried the method hint, was counted as unresolved, and added the call
        graph's blind spots, so a test-only change read as ``degraded`` structural evidence.
        """
        root, index_path, diffs = collected_test_project

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diffs["tests"]))

        (module,) = data["changed_modules"]
        assert module["fn_rdeps"] == [
            {"qname": "tests.test_util::TestReport", "caller_count": 0},
            {"qname": "tests.test_util::TestReport.test_render", "caller_count": 0},
            {"qname": "tests.test_util::test_plain", "caller_count": 0},
        ]
        assert ("hint" in data, "not_covered" in data["index"]) == (False, False)

    def test_mixed_diff_names_only_the_production_method(self, collected_test_project, scan_query):
        """In a change touching code and its tests, only the instance-called production method stays unresolved."""
        root, index_path, diffs = collected_test_project

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diffs["mixed"]))

        hinted = [
            entry["qname"] for module in data["changed_modules"] for entry in module["fn_rdeps"] if "hint" in entry
        ]
        assert (hinted, data["hint"].rpartition(" Unresolved ")[2]) == (
            ["pkg.util::Report.render"],
            "(1): pkg.util::Report.render",
        )

    def test_answer_without_unresolved_symbol_keeps_its_shape(self, edited_project, scan_query):
        """A changed function with static callers adds neither the hint nor ``not_covered``.

        The frozen diff-impact contract stays byte-identical for every answer that has nothing unresolved to disclose.
        """
        root, index_path = edited_project

        data = _diff_impact(scan_query, root, index_path)

        assert ("hint" in data, "not_covered" in data["index"]) == (False, False)


#: Non-ASCII module path: git C-quotes it as ``"pkg/mod\\303\\251.py"`` under ``core.quotePath``.
_NON_ASCII_MODULE = "pkg/modé.py"


@pytest.fixture(name="quoted_project")
def _quoted_project(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create an indexed repository holding a non-ASCII module, with git set to C-quote such paths.

    ``core.quotePath`` is set in the repository itself, so the test never depends on the runner's global git config.
    """
    root = tmp_path / "quoted"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / _NON_ASCII_MODULE).write_text("def helper(x):\n    return x + 1\n", encoding="utf-8")
    run_git(root, "init", "-q")
    run_git(root, "config", "core.quotePath", "true")
    run_git(root, "add", ".")
    _commit(root, "base")
    run_scan(scan_index, root)
    index_path = index_path_for(root)
    assert index_path.exists()
    (root / _NON_ASCII_MODULE).write_text("def helper(x):\n    return x + 2\n", encoding="utf-8")
    return root, index_path


class TestQuotedPaths:
    """A changed non-ASCII module is mapped, never dropped because git C-quoted its path.

    Read verbatim, ``"pkg/mod\\303\\251.py"`` ends in a quote rather than ``.py``, so the module silently vanished from
    the change set and the answer still claimed to be complete.
    """

    def test_working_tree_change_maps_the_module(self, quoted_project, scan_query):
        """The working-tree listing is read NUL-separated, so the changed module keeps its real path."""
        root, index_path = quoted_project

        data = _diff_impact(scan_query, root, index_path)

        assert [entry["path"] for entry in data["changed_modules"]] == [_NON_ASCII_MODULE]

    def test_diff_file_header_is_unquoted(self, quoted_project, scan_query):
        """A real ``git diff`` with a quoted ``+++`` header maps the same module from the diff file."""
        root, index_path = quoted_project
        diff = subprocess.run(["git", "diff"], cwd=str(root), capture_output=True, check=True).stdout
        assert b'+++ "b/pkg/mod\\303\\251.py"' in diff
        diff_path = root / "pr.diff"
        diff_path.write_bytes(diff)

        data = _diff_impact(scan_query, root, index_path, "--diff-file", str(diff_path))

        assert [entry["path"] for entry in data["changed_modules"]] == [_NON_ASCII_MODULE]
        assert data["changed_modules"][0]["changed_symbols"] == ["pkg.modé::helper"]


def _impact_of_test_impact(data: dict) -> dict:
    """Return the test-impact block of a ``test-impact`` answer (the answer itself)."""
    return data


def _impact_of_diff_impact(data: dict) -> dict:
    """Return the test-impact block nested inside a ``diff-impact`` answer."""
    return data["test_impact"]


def _shell_argv(pytest_cmd: str) -> list[str]:
    """Build the host-shell argv that runs the delivered ``pytest_cmd`` under the current interpreter.

    Native Windows runs it through PowerShell (single-quoted literals); every other host through ``/bin/sh``.
    """
    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        assert shell, "native Windows acceptance requires PowerShell"
        python = "'" + sys.executable.replace("'", "''") + "'"
        return [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "& " + python + " -m " + pytest_cmd + " -p no:xdist -q",
        ]
    return ["/bin/sh", "-c", shlex.quote(sys.executable) + " -m " + pytest_cmd + " -p no:xdist -q"]


@pytest.mark.parametrize(
    ("args", "impact_of"),
    [
        pytest.param(["test-impact", "lib::helper"], _impact_of_test_impact, id="test-impact"),
        pytest.param(["diff-impact"], _impact_of_diff_impact, id="diff-impact"),
    ],
)
@pytest.mark.integration
def test_impact_command_preserves_shell_sensitive_paths(git_project, scan_index, scan_query, args, impact_of):
    """Execute the delivered command against real pytest files whose directory needs shell quoting.

    A test directory name holding spaces, an ampersand, an apostrophe and ``$`` is added to the index. Running the
    ``pytest_cmd`` that ``test-impact`` / ``diff-impact`` hand back through a real shell must still find and pass both
    test files, so a broken quoting rule fails here instead of in a user's terminal.
    """
    root, index_path = git_project
    tests_dir = root / "tests space & literal'$value"
    tests_dir.mkdir()
    test_file = tests_dir / "test_extra.py"
    test_file.write_text("import lib\n\ndef test_extra():\n    assert lib.helper(1) == 3\n")
    run_git(root, "add", ".")
    _commit(root, "extra test")
    run_scan(scan_index, root)
    (root / "lib.py").write_text(_LIB_TEMPLATE.format(offset=2))
    # Existing test must agree with the changed provider so this check isolates delivery.
    (root / "tests" / "test_lib.py").write_text("import lib\n\ndef test_helper():\n    assert lib.helper(1) == 3\n")
    impact = impact_of(run_query(scan_query, root, index_path, "--no-heal", *args))
    assert impact["test_files"] == sorted(["tests/test_lib.py", test_file.relative_to(root).as_posix()])
    env = dict(os.environ, PYTEST_ADDOPTS="", PYTHONPATH=str(root))

    result = subprocess.run(_shell_argv(impact["pytest_cmd"]), cwd=root, capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


@pytest.mark.integration
def test_tracked_module_move_preserves_staged_and_unstaged_user_bytes(tmp_path: Path) -> None:
    """The tracked-file move allowed by rename-refs needs no stash or source commit.

    Real ``git mv`` over a file with both staged and unstaged edits: the move must carry the unstaged bytes to the new
    path while the index keeps only the staged ones, so a rename never silently commits or discards a user's work.
    """
    root = tmp_path / "dirty module"
    root.mkdir()
    source = root / "old.py"
    source.write_bytes(b"base = 1\n")
    run_git(root, "init", "-q")
    run_git(root, "add", "old.py")
    _commit(root, "base")
    source.write_bytes(b"base = 1\nstaged = 2\n")
    run_git(root, "add", "old.py")
    source.write_bytes(b"base = 1\nstaged = 2\nuser_edit = 3\n")

    run_git(root, "mv", "--", "old.py", "new.py")

    assert not source.exists()
    assert (root / "new.py").read_bytes() == b"base = 1\nstaged = 2\nuser_edit = 3\n"
    staged = subprocess.run(["git", "show", ":new.py"], cwd=root, capture_output=True)
    assert staged.returncode == 0
    assert staged.stdout == b"base = 1\nstaged = 2\n"


#: How many modules call ``provider.old_name``: one more than the historical 50-row display cap.
_CALLER_COUNT = 51


def _write_callers(root: Path) -> None:
    """Write ``provider.py`` plus ``_CALLER_COUNT`` modules each importing and calling ``old_name``."""
    (root / "provider.py").write_text("def old_name():\n    return 7\n")
    for number in range(_CALLER_COUNT):
        (root / f"caller_{number}.py").write_text(
            "from provider import old_name\n\ndef run():\n    return old_name()\n"
        )


def _apply_rename(root: Path, rows: list[dict]) -> None:
    """Rename ``old_name`` to ``new_name`` in the provider and in every caller module listed in *rows*."""
    (root / "provider.py").write_text("def new_name():\n    return 7\n")
    for row in rows:
        caller = root / row["path"]
        caller.write_text(caller.read_text().replace("old_name", "new_name"))


@pytest.mark.integration
def test_complete_caller_inventory_above_fifty_remains_queryable(tmp_path, scan_index, scan_query):
    """Large caller sets are complete input to rename preflight rather than a truncated migration.

    Fifty-one modules call one provider function. The ``fn-rdeps`` inventory must list all of them; applying the rename
    to exactly that inventory must leave every caller importable and working, which a truncated list would not.
    """
    root = tmp_path / "many_callers"
    root.mkdir()
    _write_callers(root)
    run_scan(scan_index, root)
    data = run_query(scan_query, root, index_path_for(root), "--no-heal", "fn-rdeps", "provider::old_name")
    assert data["count"] == _CALLER_COUNT
    assert {row["path"] for row in data["called_by"]} == {f"caller_{number}.py" for number in range(_CALLER_COUNT)}
    # Apply the complete current inventory, then exercise every changed caller with its provider.
    _apply_rename(root, data["called_by"])

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import importlib; assert all(importlib.import_module(f'caller_{{n}}').run() == 7 for n in range({_CALLER_COUNT}))",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
