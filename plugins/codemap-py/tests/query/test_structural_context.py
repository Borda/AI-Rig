"""Structural context at a verified head: out-of-tree index build, closed unavailability causes, text-search fallback.

The happy and refusal paths drive ``codemap-py context`` over a real git repository and a real detached worktree,
because the index build, the query and the "nothing written inside the reviewed tree" guarantee are the contract. Child
failures that cannot be produced on demand (a scan timing out, a busy writer lease, a crashing scanner) go through the
injected runner, the only boundary replaced.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from _scan_helpers import init_repo, run_git
from codemap_py import cli, structural_context
from codemap_py.structural_context import ContextCause, ContextRequest, ContextStatus, parse_change_set

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _git_out(root: Path, *args: str) -> str:
    """Return stripped stdout of one git command run inside *root*."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def _write(root: Path, files: Mapping[str, str]) -> None:
    """Write each relative path's text under *root*, creating parent directories."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


@pytest.fixture
def reviewed_head(tmp_path: Path) -> dict[str, Path | str]:
    """Build a repository with a base and a head commit, and a detached worktree at the head outside the repository.

    The head changes ``pkg.core.helper`` and adds ``pkg.user`` that imports and calls it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    _write(repo, {"pkg/__init__.py": "", "pkg/core.py": "def helper():\n    return 1\n"})
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "base")
    base = _git_out(repo, "rev-parse", "HEAD")
    _write(
        repo,
        {
            "pkg/core.py": "def helper():\n    return 2\n",
            "pkg/user.py": "from pkg.core import helper\n\n\ndef use():\n    return helper()\n",
        },
    )
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "head")
    head = _git_out(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    run_git(repo, "worktree", "add", "-q", "--detach", str(worktree), head)
    diff_file = tmp_path / "run" / "pr.diff"
    diff_file.parent.mkdir()
    diff_file.write_text(_git_out(worktree, "diff", f"{base}..{head}") + "\n", encoding="utf-8")
    return {"repo": repo, "worktree": worktree, "base": base, "head": head, "diff": diff_file, "tmp": tmp_path}


def _request(fixture: Mapping[str, Path | str], **overrides: object) -> ContextRequest:
    """Return a request for the fixture's worktree, index base under the fixture's temp dir, with overrides."""
    values: dict[str, object] = {
        "root": Path(fixture["worktree"]).resolve(),
        "diff_file": Path(fixture["diff"]).resolve(),
        "index_base": Path(fixture["tmp"]).resolve() / "index-base",
        "plugin_root": _PLUGIN_ROOT,
        "expect_head": str(fixture["head"]),
        "timeout_s": 120,
    }
    values.update(overrides)
    return ContextRequest(**values)


def _assert_tree_untouched(fixture: Mapping[str, Path | str]) -> None:
    """Assert neither the worktree nor the main checkout gained a file, ignored ones included."""
    for key in ("worktree", "repo"):
        root = Path(fixture[key])
        assert _git_out(root, "status", "--porcelain", "--ignored") == "", key
        assert not (root / ".cache").exists(), key


class TestIndexAtHead:
    """The index is built for the reviewed head, in a directory outside the reviewed tree."""

    def test_builds_index_outside_tree_and_reports_diff_impact(self, reviewed_head: dict) -> None:
        """A first run builds the index outside the worktree and returns the change set's blast radius."""
        record = structural_context.resolve_context(_request(reviewed_head))

        assert record["status"] == ContextStatus.AVAILABLE.value, record["detail"]
        assert record["cause"] is None
        assert record["head"] == reviewed_head["head"]
        assert record["index"]["action"] == "built"
        index_path = Path(record["index"]["path"])
        assert index_path.is_file()
        assert index_path.is_relative_to(Path(reviewed_head["tmp"]).resolve() / "index-base")
        modules = {entry["module"]: entry for entry in record["diff_impact"]["changed_modules"]}
        assert modules["pkg.core"]["importers"] == ["pkg.user"]
        assert record["method"] == "static-ast"
        assert record["confidence"] == "exact"
        assert record["query_complete"] is True
        assert record["fallback"] is None
        assert json.loads(index_path.read_text(encoding="utf-8"))["git_sha"] == reviewed_head["head"]
        _assert_tree_untouched(reviewed_head)

    def test_second_run_refreshes_the_same_index(self, reviewed_head: dict) -> None:
        """A rerun at the same head refreshes the index it built instead of building another."""
        first = structural_context.resolve_context(_request(reviewed_head))
        second = structural_context.resolve_context(_request(reviewed_head))

        assert second["status"] == ContextStatus.AVAILABLE.value, second["detail"]
        assert second["index"]["action"] == "refreshed"
        assert second["index"]["path"] == first["index"]["path"]
        _assert_tree_untouched(reviewed_head)

    def test_wrong_checkout_is_refused_before_any_index_work(self, reviewed_head: dict) -> None:
        """A root checked out at another commit names head-mismatch and builds nothing, searches nothing."""
        record = structural_context.resolve_context(_request(reviewed_head, expect_head=str(reviewed_head["base"])))

        assert record["status"] == ContextStatus.UNAVAILABLE.value
        assert record["cause"] == ContextCause.HEAD_MISMATCH.value
        assert record["fallback"] is None
        assert record["index"] is None
        assert not (Path(reviewed_head["tmp"]) / "index-base").exists()

    @pytest.mark.integration
    def test_head_verification_leaves_the_git_index_untouched(
        self, reviewed_head: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verifying a clean source file with changed stat metadata leaves Git's cached metadata alone."""
        monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
        root = Path(reviewed_head["worktree"])
        index = root / _git_out(root, "rev-parse", "--git-path", "index")
        before = index.read_bytes()
        source = root / "pkg" / "core.py"
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns - 2_000_000_000))

        record = structural_context.resolve_context(_request(reviewed_head, build=False))

        assert record["cause"] == ContextCause.INDEX_MISSING.value, record["detail"]
        assert index.read_bytes() == before

    @pytest.mark.parametrize(
        "files",
        [
            pytest.param({"pkg/core.py": "def helper():\n    return 3\n"}, id="modified-tracked-module"),
            pytest.param({"pkg/extra.py": "def more():\n    return 4\n"}, id="untracked-module"),
        ],
    )
    def test_uncommitted_python_change_is_not_the_expected_head(self, reviewed_head: dict, files: dict) -> None:
        """A root at the expected commit whose indexed Python differs from that commit is refused as head-mismatch.

        The scanner indexes the working tree, so an edited or added module would be credited to the reviewed commit.
        """
        _write(Path(reviewed_head["worktree"]), files)
        record = structural_context.resolve_context(_request(reviewed_head))

        assert (record["status"], record["cause"]) == (
            ContextStatus.UNAVAILABLE.value,
            ContextCause.HEAD_MISMATCH.value,
        )
        assert "uncommitted" in record["detail"]
        assert (record["fallback"], record["index"]) == (None, None)

    def test_untracked_files_the_scanner_never_reads_keep_the_head_exact(self, reviewed_head: dict) -> None:
        """Review artifacts and notes outside the indexed Python (dot-directories, non-Python files) change nothing."""
        _write(Path(reviewed_head["worktree"]), {".reports/run/claim-bench.py": "x = 1\n", "notes.md": "draft\n"})
        record = structural_context.resolve_context(_request(reviewed_head))

        assert (record["status"], record["confidence"]) == (ContextStatus.AVAILABLE.value, "exact"), record["detail"]

    def test_git_ignored_python_is_still_indexed_source(self, reviewed_head: dict) -> None:
        """Refuse a commit claim when an ignored but discoverable Python module is present."""
        root = Path(reviewed_head["worktree"])
        _write(root, {".gitignore": "generated/\n", "generated/helper.py": "from pkg.core import helper\n"})
        record = structural_context.resolve_context(_request(reviewed_head))

        assert record["cause"] == ContextCause.HEAD_MISMATCH.value
        assert "generated/helper.py" in record["detail"]
        assert (record["confidence"], record["fallback"]) == (None, None)

    def test_unreadable_working_tree_state_is_not_verified(
        self, reviewed_head: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the clean-tree probe itself fails, the record never claims the expected head's tree."""
        real_run = subprocess.run

        def failing_status(argv: Sequence[str], *args: object, **kwargs: object) -> object:
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 128, "", "fatal: index file corrupt")
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(structural_context.subprocess, "run", failing_status)
        record = structural_context.resolve_context(_request(reviewed_head))

        assert (record["status"], record["cause"]) == (
            ContextStatus.UNAVAILABLE.value,
            ContextCause.HEAD_MISMATCH.value,
        )

    def test_cli_writes_the_record_and_exits_zero(self, reviewed_head: dict, capsys: pytest.CaptureFixture) -> None:
        """The dispatcher's context command prints the record and writes the same record to --out."""
        out = Path(reviewed_head["tmp"]) / "run" / "codemap-context.json"
        argv = ["context", "--root", str(reviewed_head["worktree"]), "--diff-file", str(reviewed_head["diff"])]
        argv += ["--index-dir", str(Path(reviewed_head["tmp"]) / "index-base"), "--out", str(out)]
        argv += ["--expect-head", str(reviewed_head["head"])[:12]]

        assert cli.main(argv, _PLUGIN_ROOT) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed == json.loads(out.read_text(encoding="utf-8"))
        assert printed["schema"] == structural_context.SCHEMA
        assert printed["schema_version"] == structural_context.SCHEMA_VERSION
        assert printed["status"] == ContextStatus.AVAILABLE.value


class TestUnavailableCauses:
    """Every unavailable or not-applicable record names exactly one closed cause."""

    def test_index_dir_inside_root_is_a_usage_error(self, reviewed_head: dict, capsys: pytest.CaptureFixture) -> None:
        """An index base inside the reviewed tree is refused before anything is written."""
        inside = Path(reviewed_head["worktree"]) / ".cache" / "ctx"
        argv = ["--root", str(reviewed_head["worktree"]), "--diff-file", str(reviewed_head["diff"])]

        assert structural_context.main([*argv, "--index-dir", str(inside)], _PLUGIN_ROOT) == 2
        assert json.loads(capsys.readouterr().err)["error"] == "invalid_context_request"
        _assert_tree_untouched(reviewed_head)

    def test_diff_without_python_is_not_applicable(self, reviewed_head: dict) -> None:
        """A change set naming no .py path is not a structural question."""
        diff = Path(reviewed_head["tmp"]) / "run" / "docs.diff"
        diff.write_text("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n")

        record = structural_context.resolve_context(_request(reviewed_head, diff_file=diff))

        assert record["status"] == ContextStatus.NOT_APPLICABLE.value
        assert record["cause"] == ContextCause.NON_PYTHON_SCOPE.value

    @pytest.mark.parametrize(
        ("override", "cause"),
        [
            pytest.param("diff_file", ContextCause.DIFF_UNAVAILABLE, id="missing-diff"),
            pytest.param("root", ContextCause.SOURCE_UNAVAILABLE, id="missing-root"),
        ],
    )
    def test_unusable_inputs_name_their_cause(self, reviewed_head: dict, override: str, cause: ContextCause) -> None:
        """A missing diff or root is reported as such, with no fallback over an unverified tree."""
        missing = Path(reviewed_head["tmp"]) / "does-not-exist"

        record = structural_context.resolve_context(_request(reviewed_head, **{override: missing}))

        assert record["status"] == ContextStatus.UNAVAILABLE.value
        assert record["cause"] == cause.value
        assert record["fallback"] is None

    def test_frozen_index_absent_reports_index_missing_with_fallback(self, reviewed_head: dict) -> None:
        """With building disabled and no index, the record says index-missing and carries the text search."""
        record = structural_context.resolve_context(_request(reviewed_head, build=False))

        assert record["cause"] == ContextCause.INDEX_MISSING.value
        assert record["method"] == "text-search"
        assert record["confidence"] == "low"
        assert record["query_complete"] is False
        modules = {entry["module"]: entry for entry in record["fallback"]["modules"]}
        assert modules["pkg.core"]["importer_files"] == ["pkg/user.py"]
        symbols = {(entry["path"], entry["name"]): entry for entry in record["fallback"]["symbols"]}
        assert symbols[("pkg/core.py", "helper")]["reference_files"] == ["pkg/user.py"]
        _assert_tree_untouched(reviewed_head)

    def test_scan_no_autobuild_disables_building(self, reviewed_head: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        """An inherited SCAN_NO_AUTOBUILD=1 freeze is honoured: the request never builds."""
        monkeypatch.setenv("SCAN_NO_AUTOBUILD", "1")
        args = structural_context._parser().parse_args(
            ["--root", str(reviewed_head["worktree"]), "--diff-file", str(reviewed_head["diff"])]
        )

        assert structural_context.build_request(args, _PLUGIN_ROOT).build is False


def _fake_runner(build: subprocess.CompletedProcess[str] | Exception, query: object = None):
    """Return a runner that answers the index build with *build* and the query with *query*.

    A successful build writes a minimal index recording no commit, so the query step is reached.
    """

    def run(argv: Sequence[str], env: Mapping[str, str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
        """Serve one child call from the canned outcomes."""
        if Path(argv[1]).name == "scan-index":
            if isinstance(build, Exception):
                raise build
            if build.returncode == 0:
                index = Path(env["CODEMAP_INDEX_DIR"]) / f"{Path(cwd).name}.json"
                index.parent.mkdir(parents=True, exist_ok=True)
                index.write_text("{}", encoding="utf-8")
            return build
        if isinstance(query, Exception):
            raise query
        return subprocess.CompletedProcess(argv, 0, json.dumps(query), "")

    return run


_OK = subprocess.CompletedProcess(["scan-index"], 0, "", "")


class TestChildFailures:
    """Build and query failures map to their cause from what the child printed, and always carry the fallback."""

    @pytest.mark.parametrize(
        ("build", "query", "cause"),
        [
            pytest.param(subprocess.TimeoutExpired("scan-index", 1), None, "codemap-timeout", id="build-deadline"),
            pytest.param(
                subprocess.CompletedProcess([], 2, "", "scan-index: timed out after 5s"),
                None,
                "codemap-timeout",
                id="scan-own-timeout-exit-2",
            ),
            pytest.param(
                subprocess.CompletedProcess([], 1, "", '{"error": "index_busy", "detail": "writer"}'),
                None,
                "index-busy",
                id="writer-lease-held",
            ),
            pytest.param(
                subprocess.CompletedProcess([], 1, "", "Traceback ...\nRecursionError: deep"),
                None,
                "index-build-failed",
                id="scanner-crash",
            ),
            pytest.param(_OK, {"error": "unreadable_diff"}, "query-failed", id="query-error-answer"),
            pytest.param(_OK, subprocess.TimeoutExpired("scan-query", 1), "codemap-timeout", id="query-deadline"),
            pytest.param(_OK, {"index": {"stale": True}}, "index-stale", id="stale-answer"),
            pytest.param(_OK, {"index": {"root_mismatch": True}}, "index-root-mismatch", id="foreign-index"),
        ],
    )
    def test_failure_maps_to_cause_with_fallback(self, reviewed_head: dict, build, query, cause: str) -> None:
        """Each child outcome yields its closed cause, a bounded detail, and the low-confidence text search."""
        record = structural_context.resolve_context(_request(reviewed_head), runner=_fake_runner(build, query))

        assert record["status"] == ContextStatus.UNAVAILABLE.value
        assert record["cause"] == cause
        assert 0 < len(record["detail"]) <= 400
        assert record["fallback"]["method"] == "text-search"
        assert record["fallback"]["query_complete"] is False

    def test_incomplete_answer_is_degraded_not_unavailable(self, reviewed_head: dict) -> None:
        """An index answer with an unmapped changed file is used, marked degraded with the reason."""
        answer = {"changed_modules": [], "unmapped_files": ["pkg/new.py"], "index": {"query_complete": True}}

        record = structural_context.resolve_context(_request(reviewed_head), runner=_fake_runner(_OK, answer))

        assert record["status"] == ContextStatus.DEGRADED.value
        assert record["cause"] is None
        assert record["degraded_reasons"] == ["1 changed Python file(s) not in the index"]
        assert record["fallback"] is None

    def test_renamed_away_module_degrades_a_complete_answer(self, reviewed_head: dict) -> None:
        """A complete answer over a diff that renames a module away is degraded: its old importers were not checked."""
        diff = Path(reviewed_head["tmp"]) / "run" / "rename.diff"
        diff.write_text("diff --git a/pkg/core.py b/pkg/base.py\nrename from pkg/core.py\nrename to pkg/base.py\n")
        answer = {"changed_modules": [], "unmapped_files": [], "index": {"query_complete": True}}

        record = structural_context.resolve_context(
            _request(reviewed_head, diff_file=diff), runner=_fake_runner(_OK, answer)
        )

        assert record["status"] == ContextStatus.DEGRADED.value
        assert record["degraded_reasons"] == [
            "1 deleted or renamed-away Python module(s) not analysed (importers unchecked): pkg/core.py"
        ]

    def test_failing_text_search_keeps_cause_and_says_why(
        self, reviewed_head: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tree whose walk fails still yields the record, with the cause and the reason no fallback came."""

        def broken_walk(root: Path, exclusions: object) -> tuple[list[Path], dict[str, int]]:
            """Fail like an unreadable directory during the walk."""
            raise PermissionError("denied")

        monkeypatch.setattr(structural_context, "_iter_python_files", broken_walk)
        failed = subprocess.CompletedProcess([], 1, "", "Traceback ...\nOSError: unreadable")

        record = structural_context.resolve_context(_request(reviewed_head), runner=_fake_runner(failed))

        assert record["cause"] == ContextCause.INDEX_BUILD_FAILED.value
        assert record["fallback"] is None
        assert record["fallback_error"].startswith("text search failed: PermissionError")


class TestTextSearchFallback:
    """The fallback is deterministic, resolves relative imports, and flags every cap it hits."""

    @pytest.fixture
    def package_tree(self, tmp_path: Path) -> Path:
        """Write a package whose importers reach ``pkg.sub.a`` in five different spellings."""
        _write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/sub/__init__.py": "",
                "pkg/sub/a.py": "def thing():\n    return 1\n",
                "pkg/sub/b.py": "from . import a\n",
                "pkg/c.py": "from .sub.a import thing\n",
                "pkg/d.py": "import pkg.sub.a as alias\n",
                "pkg/e.py": "import pkg\n\n\ndef go():\n    return pkg.sub.a.thing()\n",
                "pkg/f.py": "class K:\n    def run(self):\n        return self.thing()\n",
                "pkg/g.py": "import pkgsub.a\nx = 'pkg.sub.abc'\n",
            },
        )
        return tmp_path

    def _change(self) -> structural_context.ChangeSet:
        """Return a change set that edits ``thing`` in ``pkg/sub/a.py``."""
        return parse_change_set(
            "diff --git a/pkg/sub/a.py b/pkg/sub/a.py\n--- a/pkg/sub/a.py\n+++ b/pkg/sub/a.py\n"
            "@@ -1,2 +1,2 @@ def thing():\n-    return 1\n+    return 2\n"
        )

    def test_importers_and_references_found_by_every_spelling(self, package_tree: Path) -> None:
        """Relative, absolute, aliased and attribute-path imports all count; look-alike names do not."""
        result = structural_context.text_search_fallback(package_tree, self._change())

        module = result["modules"][0]
        assert module["module"] == "pkg.sub.a"
        assert module["importer_files"] == ["pkg/c.py", "pkg/d.py", "pkg/e.py", "pkg/sub/b.py"]
        symbol = result["symbols"][0]
        assert (symbol["path"], symbol["name"]) == ("pkg/sub/a.py", "thing")
        assert symbol["reference_files"] == ["pkg/c.py", "pkg/e.py", "pkg/f.py"]
        assert result["not_covered"]

    def test_output_is_identical_across_runs(self, package_tree: Path) -> None:
        """Two searches over the same tree and change set return the same record."""
        first = structural_context.text_search_fallback(package_tree, self._change())

        assert structural_context.text_search_fallback(package_tree, self._change()) == first

    def test_listing_cap_sets_truncated_flag(self, package_tree: Path) -> None:
        """A listing cut by its cap keeps the full count and says it was truncated."""
        limits = structural_context.FallbackLimits(max_listed=1)

        module = structural_context.text_search_fallback(package_tree, self._change(), limits)["modules"][0]

        assert module["importer_files"] == ["pkg/c.py"]
        assert module["importer_file_count"] == 4
        assert module["importer_truncated"] is True


class TestChangeSetParsing:
    """Deleted and renamed modules stay in the change set, because their importers are the impact."""

    def test_deleted_module_is_kept_with_its_symbols(self) -> None:
        """A deleted file is listed by its old path with deleted set."""
        diff = (
            "diff --git a/pkg/old.py b/pkg/old.py\ndeleted file mode 100644\n--- a/pkg/old.py\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-def gone():\n-    return 1\n"
        )

        assert parse_change_set(diff).files == (structural_context.ChangedFile("pkg/old.py", True, ("gone",)),)

    def test_pure_rename_keeps_both_sides(self) -> None:
        """A rename without hunks lists the new path and the old path as deleted."""
        diff = "diff --git a/pkg/a.py b/pkg/b.py\nsimilarity index 100%\nrename from pkg/a.py\nrename to pkg/b.py\n"

        assert parse_change_set(diff).python_paths == ("pkg/a.py", "pkg/b.py")
        assert [changed.deleted for changed in parse_change_set(diff).files] == [True, False]

    def test_removed_line_starting_with_dashes_is_content(self) -> None:
        """A removed line whose text begins with two dashes is hunk content, not a new file header."""
        diff = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,1 @@\n--- not a header\n x = 1\n"

        assert parse_change_set(diff).python_paths == ("m.py",)
