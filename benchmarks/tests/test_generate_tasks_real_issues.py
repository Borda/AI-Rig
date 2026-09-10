"""Tests for pure helper functions in generate-tasks-real-issues.py.

Only tests functions that require no network access:
    difficulty_for, module_for, build_task, build_prompt,
    is_meaningful_issue, _is_test_path.

All tests follow Arrange-Act-Assert and parametrize over documented
variants, boundary values, and adversarial inputs.
"""

from __future__ import annotations

from typing import Any

import pytest


GENERIC_TITLES = ("bug", "question", "help", "feature request", "feature", "issue", "error")


# ===========================================================================
# Fixtures
# ===========================================================================


def _make_pr(
    mod: Any,
    *,
    number: int = 99,
    source_files: list[str] | None = None,
    py_file_count: int = 1,
    closes_issue: bool = False,
    source_changes: dict[str, int] | None = None,
) -> Any:
    """Build a minimal PullRequestInfo for tests.

    Args:
        mod: Loaded generate-tasks-real-issues module.
        number: PR number.
        source_files: Source files list; defaults to a single placeholder path.
        py_file_count: Total Python file count (tests included).
        closes_issue: Whether the PR explicitly closes its parent issue.
        source_changes: Per-path change size; ``None`` leaves it unset (no signal).

    Returns:
        A PullRequestInfo instance.

    Example:
        >>> mod = getfixture("script_gen_real_issues")
        >>> pr = _make_pr(mod, number=7, source_files=[], py_file_count=0)
        >>> pr.number, pr.source_files, pr.py_file_count
        (7, [], 0)
    """
    if source_files is None:
        source_files = ["src/lightning/pytorch/trainer/trainer.py"]
    return mod.PullRequestInfo(
        number=number,
        source_files=source_files,
        py_file_count=py_file_count,
        closes_issue=closes_issue,
        source_changes=source_changes,
    )


def _make_record(
    mod: Any,
    *,
    number: int = 1,
    title: str = "Fix something specific",
    body: str = "Detailed description of the issue.",
    pr: Any | None = None,
) -> Any:
    """Build a minimal IssueRecord for tests.

    Args:
        mod: Loaded generate-tasks-real-issues module.
        number: Issue number.
        title: Issue title.
        body: Issue body text.
        pr: PullRequestInfo; defaults to a one-file PR.

    Returns:
        An IssueRecord instance.

    Example:
        >>> mod = getfixture("script_gen_real_issues")
        >>> record = _make_record(mod, number=7, title="Example fix")
        >>> record.number, record.title, record.pr.number
        (7, 'Example fix', 99)
    """
    if pr is None:
        pr = _make_pr(mod)
    return mod.IssueRecord(number=number, title=title, body=body, pr=pr)


# ===========================================================================
# class TestDifficultyFor
# ===========================================================================


class TestDifficultyFor:
    """Tests for difficulty_for(file_count: int) -> str.

    Contract (docstring): returns "simple" for count ≤ 1, "medium" for 2–3, "hard" for 4+.
    """

    @pytest.mark.parametrize(
        "file_count,expected",
        [
            pytest.param(1, "simple", id="1"),  # documented boundary: exactly 1 -> simple
            pytest.param(2, "medium", id="2"),  # lower boundary of medium band
            pytest.param(3, "medium", id="3"),  # upper boundary of medium band
            pytest.param(4, "hard", id="4"),  # first hard value
            pytest.param(5, "hard", id="5"),  # another hard value
            pytest.param(10, "hard", id="10"),  # large collection -> hard
        ],
    )
    def test_difficulty_for_documented_ranges(
        self, script_gen_real_issues: Any, file_count: int, expected: str
    ) -> None:
        """Verify all documented difficulty bands and their boundary values.

        Args:
            file_count: Number of source files.
            expected: Expected difficulty label.
        """
        assert script_gen_real_issues.difficulty_for(file_count) == expected

    def test_difficulty_for_zero_is_simple(self, script_gen_real_issues: Any) -> None:
        """Verify that zero files yields simple (boundary below min advertised value).

        Scenario: zero source files — unusual but not excluded by type hint;
        the documented "≤ 1" guard makes this simple.
        """
        assert script_gen_real_issues.difficulty_for(0) == "simple"

    @pytest.mark.parametrize("count", (1, 2, 4))
    def test_difficulty_for_returns_string(self, script_gen_real_issues: Any, count: int) -> None:
        """Verify return type is always str for representative inputs.

        Scenario: caller downstream writes the value into a JSON dict without
        type-checking; must be a plain str, not e.g. an Enum.
        """
        result = script_gen_real_issues.difficulty_for(count)
        assert isinstance(result, str), f"expected str for count={count}, got {type(result)}"


# ===========================================================================
# class TestIsTestPath
# ===========================================================================


class TestIsTestPath:
    """Tests for _is_test_path(path: str) -> bool.

    Contract (source + module docstring): returns True when the path belongs to a test file — detected via directory
    segment "tests"/"test", a test_* prefix, _test.py suffix, or conftest.py filename.
    """

    @pytest.mark.parametrize(
        "path,expected",
        [
            # --- test directory segment ---
            pytest.param("tests/unit/test_trainer.py", True, id="tests-unit-test_trainer.py"),
            pytest.param("src/lightning/test/helpers.py", True, id="src-lightning-test-helpers.py"),
            pytest.param("a/tests/b/c.py", True, id="a-tests-b-c.py"),  # segment anywhere in path
            # --- test_ prefix on filename ---
            pytest.param("test_trainer.py", True, id="test_trainer.py"),  # top-level file
            pytest.param("src/lightning/test_loader.py", True, id="src-lightning-test_loader.py"),
            # --- _test.py suffix ---
            pytest.param("src/lightning/trainer_test.py", True, id="src-lightning-trainer_test.py"),
            # --- conftest.py ---
            pytest.param("conftest.py", True, id="conftest.py"),
            pytest.param("src/conftest.py", True, id="src-conftest.py"),
            # --- normal source files (must NOT match) ---
            pytest.param(
                "src/lightning/pytorch/trainer/trainer.py", False, id="src-lightning-pytorch-trainer-trainer.py"
            ),
            pytest.param(
                "lightning/pytorch/utilities/combined_loader.py",
                False,
                id="lightning-pytorch-utilities-combined_loader.py",
            ),
            pytest.param("setup.py", False, id="setup.py"),
            pytest.param("atestfile.py", False, id="atestfile.py"),  # 'atest' prefix, no separator
            pytest.param(
                "src/lightning/latest_test_results.py", False, id="src-lightning-latest_test_results.py"
            ),  # does not end _test.py
        ],
    )
    def test_is_test_path_classification(self, script_gen_real_issues: Any, path: str, expected: bool) -> None:
        """Verify test-path detection across all documented heuristics and boundary paths.

        Args:
            path: Repo-relative source path.
            expected: Expected classification result.
        """
        assert script_gen_real_issues._is_test_path(path) is expected, f"_is_test_path({path!r}) expected {expected}"

    def test_is_test_path_conftest_in_subdir(self, script_gen_real_issues: Any) -> None:
        """Verify conftest.py inside a nested package directory is flagged as test.

        Scenario: user has conftest.py anywhere in path; harness must exclude it
        from ground-truth source files.
        """
        assert script_gen_real_issues._is_test_path("src/lightning/pytorch/conftest.py") is True

    def test_is_test_path_returns_bool(self, script_gen_real_issues: Any) -> None:
        """Verify the return value is a strict bool, not a truthy/falsy object.

        Scenario: caller uses ``is True`` comparisons and stores in JSON where
        type matters.
        """
        result = script_gen_real_issues._is_test_path("src/foo.py")
        assert result is False
        result_test = script_gen_real_issues._is_test_path("tests/foo.py")
        assert result_test is True


# ===========================================================================
# class TestModuleFor
# ===========================================================================


class TestModuleFor:
    """Tests for module_for(path: str) -> str.

    Contract (docstring): strips leading ``src/``, collapses ``__init__.py`` to package name, drops ``.py``, replaces
    ``/`` with ``.``.
    """

    @pytest.mark.parametrize(
        "path,expected",
        [
            # --- src/ prefix stripping ---
            pytest.param(
                "src/lightning/pytorch/trainer/trainer.py",
                "lightning.pytorch.trainer.trainer",
                id="src-lightning-pytorch-trainer-trainer.py",
            ),
            # --- no src/ prefix ---
            pytest.param(
                "lightning/pytorch/callbacks/timer.py",
                "lightning.pytorch.callbacks.timer",
                id="lightning-pytorch-callbacks-timer.py",
            ),
            # --- __init__.py collapses to package ---
            pytest.param(
                "src/lightning/pytorch/__init__.py", "lightning.pytorch", id="src-lightning-pytorch-__init__.py"
            ),
            pytest.param(
                "lightning/pytorch/utilities/__init__.py",
                "lightning.pytorch.utilities",
                id="lightning-pytorch-utilities-__init__.py",
            ),
            # --- top-level single file ---
            pytest.param("setup.py", "setup", id="setup.py"),
            # --- non-root src/ segment: not stripped, but slashes still become dots ---
            pytest.param(
                "pkg/src/module.py",
                "pkg.src.module",  # src/ not at root -> not stripped; / -> . applies everywhere
                id="pkg-src-module.py",
            ),
        ],
    )
    def test_module_for_conversion(self, script_gen_real_issues: Any, path: str, expected: str) -> None:
        """Verify dotted module name conversion for all documented path patterns.

        Args:
            path: Repo-relative source path.
            expected: Expected dotted module name.
        """
        assert script_gen_real_issues.module_for(path) == expected

    def test_module_for_src_only_at_root(self, script_gen_real_issues: Any) -> None:
        """Verify that 'src/' inside a path but not at root is NOT stripped.

        Scenario: repo has a 'pkg/src/module.py' layout; only a leading 'src/'
        is stripped per documented contract. Remaining slashes are converted to
        dots (documented: "replaces path separators with dots").
        """
        result = script_gen_real_issues.module_for("pkg/src/module.py")
        # path does NOT start with "src/" so no prefix is stripped;
        # but '/' -> '.' replacement still applies to all remaining separators.
        assert result == "pkg.src.module"

    def test_module_for_returns_str(self, script_gen_real_issues: Any) -> None:
        """Verify return type is always str.

        Scenario: downstream JSON serialization assumes str.
        """
        assert isinstance(script_gen_real_issues.module_for("src/foo/bar.py"), str)


# ===========================================================================
# class TestBuildPrompt
# ===========================================================================


class TestBuildPrompt:
    """Tests for build_prompt(title, body) -> str.

    Contract (docstring): returns title + blank line + truncated body; body truncated to PROMPT_BODY_LIMIT characters;
    whitespace stripped.
    """

    def test_build_prompt_basic_structure(self, script_gen_real_issues: Any) -> None:
        """Verify that title and body are separated by exactly one blank line.

        Scenario: standard issue title + body produces a two-section prompt.
        """
        result = script_gen_real_issues.build_prompt("Fix the trainer", "Some body text.")
        parts = result.split("\n\n")
        assert len(parts) == 2, f"expected exactly two sections separated by blank line, got: {result!r}"
        assert parts[0] == "Fix the trainer"
        assert parts[1] == "Some body text."

    def test_build_prompt_body_truncated_at_limit(self, script_gen_real_issues: Any) -> None:
        """Verify body is truncated to exactly PROMPT_BODY_LIMIT chars.

        Scenario: very long body exceeds the limit; only first PROMPT_BODY_LIMIT
        chars must appear in the prompt.
        """
        long_body = "x" * (script_gen_real_issues.PROMPT_BODY_LIMIT + 500)
        result = script_gen_real_issues.build_prompt("Title", long_body)
        _, body_part = result.split("\n\n", 1)
        assert len(body_part) == script_gen_real_issues.PROMPT_BODY_LIMIT

    def test_build_prompt_body_not_truncated_when_under_limit(self, script_gen_real_issues: Any) -> None:
        """Verify short body is not truncated or padded.

        Scenario: body shorter than PROMPT_BODY_LIMIT is reproduced verbatim.
        """
        short_body = "Short description."
        result = script_gen_real_issues.build_prompt("Title", short_body)
        _, body_part = result.split("\n\n", 1)
        assert body_part == short_body

    def test_build_prompt_strips_title_whitespace(self, script_gen_real_issues: Any) -> None:
        """Verify surrounding whitespace on the title is stripped.

        Scenario: raw issue title from API may have leading/trailing spaces.
        """
        result = script_gen_real_issues.build_prompt("  Padded title  ", "body")
        first_line = result.split("\n\n")[0]
        assert first_line == "Padded title"

    def test_build_prompt_strips_body_whitespace(self, script_gen_real_issues: Any) -> None:
        """Verify surrounding whitespace on the body is stripped before truncation.

        Scenario: raw issue body from API may have leading/trailing newlines.
        """
        result = script_gen_real_issues.build_prompt("Title", "\n\n  Body text.  \n")
        _, body_part = result.split("\n\n", 1)
        assert body_part.startswith("Body text.")

    def test_build_prompt_exact_limit_body_unchanged(self, script_gen_real_issues: Any) -> None:
        """Verify body exactly at PROMPT_BODY_LIMIT is reproduced without change.

        Scenario: boundary value — no off-by-one truncation.
        """
        exact_body = "a" * script_gen_real_issues.PROMPT_BODY_LIMIT
        result = script_gen_real_issues.build_prompt("T", exact_body)
        _, body_part = result.split("\n\n", 1)
        assert len(body_part) == script_gen_real_issues.PROMPT_BODY_LIMIT
        assert body_part == exact_body


# ===========================================================================
# class TestIsMeaningfulIssue
# ===========================================================================


class TestIsMeaningfulIssue:
    """Tests for is_meaningful_issue(title, body) -> bool.

    Contract (docstring): returns True only when title is non-empty, not in GENERIC_TITLES, and body is non-trivial
    (truthy and non-blank).
    """

    @pytest.mark.parametrize(
        "title,body,expected",
        [
            # --- happy path: specific title + real body ---
            pytest.param(
                "CombinedLoader hangs on StopIteration",
                "Detailed description here.",
                True,
                id="combinedloader-hangs-on-stopiteration",
            ),
            pytest.param(
                "Timer callback resets on resume",
                "Steps to reproduce:\n1. ...",
                True,
                id="timer-callback-resets-on-resume",
            ),
            # --- generic titles are case-folded before the GENERIC_TITLES membership check;
            # the membership check itself over the full current set is proven exhaustively by
            # test_is_meaningful_issue_generic_title_exhaustive below, so only case-folding is
            # spot-checked here ---
            pytest.param("Bug", "Detailed description.", False, id="generic-title-mixed-case"),  # case folded
            pytest.param("BUG", "Detailed description.", False, id="generic-title-uppercase"),
            # --- empty / blank title ---
            pytest.param("", "Body here.", False, id="empty"),
            pytest.param("   ", "Body here.", False, id="whitespace"),  # whitespace-only title
            # --- missing / blank body ---
            pytest.param("Specific title", None, False, id="specific-title-none"),
            pytest.param("Specific title", "", False, id="specific-title-empty"),
            pytest.param("Specific title", "   ", False, id="specific-title-whitespace"),  # whitespace-only body
        ],
    )
    def test_is_meaningful_issue(
        self, script_gen_real_issues: Any, title: str, body: str | None, expected: bool
    ) -> None:
        """Verify is_meaningful_issue across all documented accept/reject cases.

        Args:
            title: Issue title.
            body: Issue body (may be None).
            expected: Expected truthiness result.
        """
        assert script_gen_real_issues.is_meaningful_issue(title, body) is expected, (
            f"is_meaningful_issue({title!r}, {body!r}) expected {expected}"
        )

    def test_is_meaningful_issue_title_with_whitespace_around_generic(self, script_gen_real_issues: Any) -> None:
        """Verify that a generic title with surrounding whitespace is rejected.

        Scenario: API may return "  bug  " — should still be filtered.
        """
        assert script_gen_real_issues.is_meaningful_issue("  bug  ", "Detailed body.") is False

    @pytest.mark.parametrize("generic", GENERIC_TITLES)
    def test_is_meaningful_issue_generic_title_exhaustive(self, script_gen_real_issues: Any, generic: str) -> None:
        """Verify every entry in GENERIC_TITLES is rejected with a valid body.

        Scenario: an added generic title must make the explicit parameter set
        fail closed until its rejection case is added.
        """
        body = "Non-trivial body that would normally qualify."
        assert set(GENERIC_TITLES) == script_gen_real_issues.GENERIC_TITLES
        assert script_gen_real_issues.is_meaningful_issue(generic, body) is False, (
            f"Generic title {generic!r} should be rejected"
        )


# ===========================================================================
# class TestBuildTask
# ===========================================================================


class TestBuildTask:
    """Tests for build_task(index, record) -> dict.

    Contract (docstring + schema): returns a task dict with keys id, type, source, workflow_subtype, difficulty,
    issue_number, issue_url, pr_number, pr_url, prompt, ground_truth, primary_module, scoreable, pr_closes_issue.
    """

    _REQUIRED_KEYS = {
        "id",
        "type",
        "source",
        "workflow_subtype",
        "difficulty",
        "issue_number",
        "issue_url",
        "pr_number",
        "pr_url",
        "prompt",
        "ground_truth",
        "primary_module",
        "scoreable",
        "pr_closes_issue",
    }

    def test_build_task_contains_all_required_keys(self, script_gen_real_issues: Any) -> None:
        """Verify every documented schema key is present in the output dict.

        Scenario: downstream harness reads each key; missing key = KeyError.
        """
        record = _make_record(script_gen_real_issues)
        task = script_gen_real_issues.build_task(1, record)
        missing = self._REQUIRED_KEYS - task.keys()
        assert not missing, f"Missing keys in task dict: {missing}"

    @pytest.mark.parametrize(
        "index,expected_id",
        [
            pytest.param(1, "OSS-01", id="1"),
            pytest.param(9, "OSS-09", id="9"),
            pytest.param(10, "OSS-10", id="10"),
            pytest.param(20, "OSS-20", id="20"),
        ],
    )
    def test_build_task_id_formatting(self, script_gen_real_issues: Any, index: int, expected_id: str) -> None:
        """Verify task id uses zero-padded two-digit ordinal.

        Args:
            index: 1-based ordinal passed to build_task.
            expected_id: Expected task id string.
        """
        record = _make_record(script_gen_real_issues)
        task = script_gen_real_issues.build_task(index, record)
        assert task["id"] == expected_id

    def test_build_task_type_and_source_are_real_issue(self, script_gen_real_issues: Any) -> None:
        """Verify provenance fields type and source are fixed to 'real_issue'.

        Scenario: harness filters tasks by type/source to determine scoring
        methodology; wrong value routes task to wrong scorer.
        """
        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues))
        assert task["type"] == "real_issue"
        assert task["source"] == "real_issue"

    def test_build_task_scoreable_is_true(self, script_gen_real_issues: Any) -> None:
        """Verify scoreable is always True for tasks built from real issue records.

        Scenario: harness skips tasks where scoreable=False; all built tasks
        have non-empty ground truth, so scoreable must be True.
        """
        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues))
        assert task["scoreable"] is True

    def test_build_task_ground_truth_structure(self, script_gen_real_issues: Any) -> None:
        """Verify ground_truth contains files_changed list and file_count int.

        Scenario: scoring harness reads both sub-keys directly.
        """
        source_files = ["src/lightning/pytorch/trainer/trainer.py", "src/lightning/pytorch/loops/fit_loop.py"]
        pr = _make_pr(script_gen_real_issues, source_files=source_files)
        record = _make_record(script_gen_real_issues, pr=pr)
        task = script_gen_real_issues.build_task(1, record)

        gt = task["ground_truth"]
        assert gt["files_changed"] == source_files
        assert gt["file_count"] == 2

    @pytest.mark.parametrize(
        "source_files,expected_difficulty",
        [
            pytest.param(["src/a.py"], "simple", id="src-a.py"),
            pytest.param(["src/a.py", "src/b.py"], "medium", id="src-a.py-src-b.py"),
            pytest.param(["src/a.py", "src/b.py", "src/c.py"], "medium", id="src-a.py-src-b.py-src-c.py"),
            pytest.param(
                ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], "hard", id="src-a.py-src-b.py-src-c.py-src-d.py"
            ),
        ],
    )
    def test_build_task_difficulty_derived_from_file_count(
        self,
        script_gen_real_issues: Any,
        source_files: list[str],
        expected_difficulty: str,
    ) -> None:
        """Verify difficulty label is derived from source file count via difficulty_for.

        Args:
            source_files: Source file list used as ground truth.
            expected_difficulty: Expected difficulty label in the task dict.
        """
        pr = _make_pr(script_gen_real_issues, source_files=source_files)
        record = _make_record(script_gen_real_issues, pr=pr)
        task = script_gen_real_issues.build_task(1, record)
        assert task["difficulty"] == expected_difficulty

    def test_build_task_primary_module_from_first_source_file(self, script_gen_real_issues: Any) -> None:
        """Verify primary_module is derived from the first source file via module_for.

        Scenario: harness uses primary_module as an index key; it must reflect
        the first file in source_files.
        """
        source_files = ["src/lightning/pytorch/trainer/trainer.py", "src/lightning/pytorch/loops/fit_loop.py"]
        pr = _make_pr(script_gen_real_issues, source_files=source_files)
        record = _make_record(script_gen_real_issues, pr=pr)
        task = script_gen_real_issues.build_task(1, record)
        assert task["primary_module"] == "lightning.pytorch.trainer.trainer"

    def test_build_task_issue_url_contains_issue_number(self, script_gen_real_issues: Any) -> None:
        """Verify issue_url encodes the issue number under the repo base URL.

        Scenario: task output links to the original issue; wrong URL breaks
        reviewer workflow.
        """
        record = _make_record(script_gen_real_issues, number=42)
        task = script_gen_real_issues.build_task(1, record)
        assert task["issue_url"] == f"{script_gen_real_issues.REPO_URL}/issues/42"
        assert task["issue_number"] == 42

    def test_build_task_pr_url_contains_pr_number(self, script_gen_real_issues: Any) -> None:
        """Verify pr_url encodes the PR number under the repo base URL.

        Scenario: reviewer follows pr_url to inspect the fix; wrong URL breaks
        that workflow.
        """
        pr = _make_pr(script_gen_real_issues, number=777)
        record = _make_record(script_gen_real_issues, pr=pr)
        task = script_gen_real_issues.build_task(1, record)
        assert task["pr_url"] == f"{script_gen_real_issues.REPO_URL}/pull/777"
        assert task["pr_number"] == 777

    def test_build_task_pr_closes_issue_propagated(self, script_gen_real_issues: Any) -> None:
        """Verify pr_closes_issue reflects PullRequestInfo.closes_issue accurately.

        Scenario: downstream quality audit checks this flag; wrong value masks
        provenance quality for the downstream audit.
        """
        pr_closes = _make_pr(script_gen_real_issues, closes_issue=True)
        pr_not_closes = _make_pr(script_gen_real_issues, closes_issue=False)

        task_closes = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues, pr=pr_closes))
        task_not = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues, pr=pr_not_closes))

        assert task_closes["pr_closes_issue"] is True
        assert task_not["pr_closes_issue"] is False

    def test_build_task_prompt_contains_title_and_body(self, script_gen_real_issues: Any) -> None:
        """Verify the prompt field is built from title and body via build_prompt.

        Scenario: agent receives the prompt string; it must contain the issue
        title as the first line followed by the body.
        """
        record = _make_record(script_gen_real_issues, title="Trainer crashes on TPU", body="Reproducible on v2.0.")
        task = script_gen_real_issues.build_task(1, record)
        prompt: str = task["prompt"]
        assert prompt.startswith("Trainer crashes on TPU")
        assert "Reproducible on v2.0." in prompt

    def test_build_task_workflow_subtype(self, script_gen_real_issues: Any) -> None:
        """Verify workflow_subtype is always 'pre_implementation_research'.

        Scenario: harness routes task to appropriate workflow based on this
        field; any other value changes the harness behavior.
        """
        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues))
        assert task["workflow_subtype"] == "pre_implementation_research"

    def test_build_task_primary_module_picks_most_changed_file(self, script_gen_real_issues: Any) -> None:
        """Verify primary_module reflects the most-changed file, not the first one.

        Scenario: a multi-file PR whose second file has the larger diff must not
        yield the first file's module as primary — the bigger diff drives it.
        """
        source_files = ["src/lightning/pytorch/trainer/trainer.py", "src/lightning/pytorch/loops/fit_loop.py"]
        changes = {source_files[0]: 3, source_files[1]: 90}
        pr = _make_pr(script_gen_real_issues, source_files=source_files, source_changes=changes)
        record = _make_record(script_gen_real_issues, pr=pr)
        task = script_gen_real_issues.build_task(1, record)
        assert task["primary_module"] == "lightning.pytorch.loops.fit_loop"
        assert task["primary_module_basis"] == "most_changed"

    def test_build_task_primary_module_basis_first_file_without_signal(self, script_gen_real_issues: Any) -> None:
        """Verify basis is 'first_file' when no change-size data is available.

        Scenario: a stub/hand-authored PR carries no additions/deletions, so the
        selection falls back to the first file and flags the arbitrariness.
        """
        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues))
        assert task["primary_module_basis"] == "first_file"


# ===========================================================================
# class TestSelectPrimaryModule
# ===========================================================================


class TestSelectPrimaryModule:
    """Tests for select_primary_module(source_files, source_changes) -> (module, basis)."""

    @pytest.mark.parametrize(
        "source_files,source_changes,expected_module,expected_basis",
        [
            # most-changed wins even when it is not first
            pytest.param(
                ["src/a.py", "src/b.py"],
                {"src/a.py": 3, "src/b.py": 40},
                "b",
                "most_changed",
                id="src-a.py-src-b.py-src-a.py-3-src-b.py-40",
            ),
            # first-file tiebreak when change sizes are equal
            pytest.param(
                ["src/a.py", "src/b.py"],
                {"src/a.py": 10, "src/b.py": 10},
                "a",
                "most_changed",
                id="src-a.py-src-b.py-src-a.py-10-src-b.py-10",
            ),
            # no signal (None) -> first file
            pytest.param(["src/a.py", "src/b.py"], None, "a", "first_file", id="src-a.py-src-b.py-none"),
            # all-zero changes -> treated as no signal -> first file
            pytest.param(
                ["src/a.py", "src/b.py"],
                {"src/a.py": 0, "src/b.py": 0},
                "a",
                "first_file",
                id="src-a.py-src-b.py-src-a.py-0-src-b.py-0",
            ),
            # single file -> that file regardless of basis
            pytest.param(["src/only.py"], {"src/only.py": 5}, "only", "most_changed", id="src-only.py"),
        ],
    )
    def test_select_primary_module(
        self,
        script_gen_real_issues: Any,
        source_files: list[str],
        source_changes: dict[str, int] | None,
        expected_module: str,
        expected_basis: str,
    ) -> None:
        """Verify most-changed selection, tiebreak, and no-signal fallback.

        Args:
            source_files: Ordered source paths.
            source_changes: Per-path change size, or None.
            expected_module: Expected dotted module name.
            expected_basis: Expected selection basis label.
        """
        module, basis = script_gen_real_issues.select_primary_module(source_files, source_changes)
        assert module == expected_module
        assert basis == expected_basis


# ===========================================================================
# class TestResolveMergedPrProvenance
# ===========================================================================


class TestResolveMergedPrProvenance:
    """Tests for the weak-provenance warning emitted by resolve_merged_pr."""

    def test_warns_when_falling_back_to_non_closing_pr(
        self, script_gen_real_issues: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A fallback to a non-closing candidate emits a visible stderr warning.

        Scenario: no cross-referenced PR carries a closes/fixes/resolves keyword,
        so the weaker timeline-only linkage must not be accepted silently.
        """
        monkeypatch.setattr(
            script_gen_real_issues,
            "_inspect_pr",
            lambda number, min_py, max_py, issue_number=None: _make_pr(
                script_gen_real_issues, number=number, closes_issue=False
            ),
        )
        result = script_gen_real_issues.resolve_merged_pr([101], 1, 5, issue_number=42)
        assert result.number == 101
        err = capsys.readouterr().err
        assert "weak provenance" in err
        assert "#42" in err

    def test_no_warning_when_closing_pr_available(
        self, script_gen_real_issues: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A candidate that explicitly closes the issue is chosen with no warning.

        Scenario: strong provenance exists, so the fallback path is never taken.
        """
        monkeypatch.setattr(
            script_gen_real_issues,
            "_inspect_pr",
            lambda number, min_py, max_py, issue_number=None: _make_pr(
                script_gen_real_issues, number=number, closes_issue=True
            ),
        )
        result = script_gen_real_issues.resolve_merged_pr([101], 1, 5, issue_number=42)
        assert result.closes_issue is True
        assert "weak provenance" not in capsys.readouterr().err


# ===========================================================================
# class TestInspectPrSourceFiltering
# ===========================================================================


class TestInspectPrSourceFiltering:
    """Tests for _inspect_pr source-file filtering using mocked GitHub data."""

    def test_filters_test_files_and_records_source_change_sizes(
        self, script_gen_real_issues: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only non-test Python files become source ground truth; change sizes are retained."""

        def _fake_gh_json(args: list[str], timeout: int = 60) -> dict:
            """Return the scenario-specific merged-PR response without contacting GitHub."""
            return {
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "body": "Fixes #42",
                "files": [
                    {"path": "src/pkg/core.py", "additions": 3, "deletions": 2},
                    {"path": "tests/test_core.py", "additions": 100, "deletions": 0},
                    {"path": "docs/guide.md", "additions": 9, "deletions": 1},
                ],
            }

        monkeypatch.setattr(script_gen_real_issues, "_gh_json", _fake_gh_json)

        result = script_gen_real_issues._inspect_pr(123, min_py=1, max_py=5, issue_number=42)

        assert result is not None
        assert result.source_files == ["src/pkg/core.py"]
        assert result.py_file_count == 2
        assert result.source_changes == {"src/pkg/core.py": 5}
        assert result.closes_issue is True

    def test_all_test_files_returns_none(self, script_gen_real_issues: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A PR with Python changes only in tests is not a source-backed task."""

        def _fake_gh_json(args: list[str], timeout: int = 60) -> dict:
            """Return the scenario-specific merged-PR response without contacting GitHub."""
            return {
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "body": "Fixes #42",
                "files": [
                    {"path": "tests/test_core.py", "additions": 10, "deletions": 0},
                    {"path": "src/pkg/core_test.py", "additions": 1, "deletions": 1},
                ],
            }

        monkeypatch.setattr(script_gen_real_issues, "_gh_json", _fake_gh_json)

        assert script_gen_real_issues._inspect_pr(123, min_py=1, max_py=5, issue_number=42) is None

    def test_no_python_files_returns_none(self, script_gen_real_issues: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A PR without Python files cannot produce Python source ground truth."""

        def _fake_gh_json(args: list[str], timeout: int = 60) -> dict:
            """Return the scenario-specific merged-PR response without contacting GitHub."""
            return {
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "body": "Fixes #42",
                "files": [{"path": "README.md", "additions": 10, "deletions": 0}],
            }

        monkeypatch.setattr(script_gen_real_issues, "_gh_json", _fake_gh_json)

        assert script_gen_real_issues._inspect_pr(123, min_py=1, max_py=5, issue_number=42) is None
