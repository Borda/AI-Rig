"""Tests for pure helper functions in generate-tasks-real-issues.py.

Only tests functions that require no network access:
    difficulty_for, module_for, build_task, build_prompt,
    is_meaningful_issue, _is_test_path.

All tests follow Arrange-Act-Assert and parametrize over documented
variants, boundary values, and adversarial inputs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import pytest

GENERIC_TITLES = ("bug", "question", "help", "feature request", "feature", "issue", "error")
TRAINER_FILE = "src/lightning/pytorch/trainer/trainer.py"
FIT_LOOP_FILE = "src/lightning/pytorch/loops/fit_loop.py"


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
        source_files = [TRAINER_FILE]
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


@pytest.fixture
def default_task(script_gen_real_issues: Any) -> dict[str, Any]:
    """Build task number 1 from a default record (one-file PR, no change-size data)."""
    return script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues))


@pytest.fixture
def stub_gh_pr(script_gen_real_issues: Any, monkeypatch: pytest.MonkeyPatch) -> Callable[[list[dict[str, Any]]], None]:
    """Return a function that makes `_gh_json` serve one merged PR ("Fixes #42") with the given files."""

    def _stub(files: list[dict[str, Any]]) -> None:
        def _fake_gh_json(args: list[str], timeout: int = 60) -> dict:
            """Return the scenario-specific merged-PR response without contacting GitHub."""
            return {"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z", "body": "Fixes #42", "files": files}

        monkeypatch.setattr(script_gen_real_issues, "_gh_json", _fake_gh_json)

    return _stub


class TestDifficultyFor:
    """Tests for difficulty_for(file_count: int) -> str.

    Contract (docstring): returns "simple" for count ≤ 1, "medium" for 2–3, "hard" for 4+.
    """

    @pytest.mark.parametrize(
        ("file_count", "expected"),
        [
            pytest.param(0, "simple", id="zero-files"),  # below the advertised minimum; the "≤ 1" guard makes it simple
            pytest.param(1, "simple", id="1"),  # documented boundary: exactly 1 -> simple
            pytest.param(2, "medium", id="2"),  # lower boundary of medium band
            pytest.param(3, "medium", id="3"),  # upper boundary of medium band
            pytest.param(4, "hard", id="4"),  # first hard value
            pytest.param(5, "hard", id="5"),  # another hard value
            pytest.param(10, "hard", id="10"),  # large collection -> hard
        ],
    )
    def test_maps_file_count_to_documented_band(
        self, script_gen_real_issues: Any, file_count: int, expected: str
    ) -> None:
        """Every documented difficulty band and its boundary values map to a plain string label.

        The label is written into JSON by downstream code without type checks, so it must be a plain ``str`` (not an
        Enum) as well as equal to the documented band name.
        """
        result = script_gen_real_issues.difficulty_for(file_count)

        assert result == expected
        assert isinstance(result, str), f"expected str for count={file_count}, got {type(result)}"


class TestIsTestPath:
    """Tests for _is_test_path(path: str) -> bool.

    Contract (source + module docstring): returns True when the path belongs to a test file — detected via directory
    segment "tests"/"test", a test_* prefix, _test.py suffix, or conftest.py filename.
    """

    @pytest.mark.parametrize(
        ("path", "expected"),
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
            pytest.param("src/lightning/pytorch/conftest.py", True, id="nested-conftest.py"),
            # --- normal source files (must NOT match) ---
            pytest.param(TRAINER_FILE, False, id="src-lightning-pytorch-trainer-trainer.py"),
            pytest.param(
                "lightning/pytorch/utilities/combined_loader.py",
                False,
                id="lightning-pytorch-utilities-combined_loader.py",
            ),
            pytest.param("setup.py", False, id="setup.py"),
            pytest.param("atestfile.py", False, id="atestfile.py"),  # 'atest' prefix, no separator
            pytest.param("src/lightning/latest_test_results.py", False, id="latest_test_results.py"),  # not _test.py
        ],
    )
    def test_classifies_paths_by_documented_heuristics(
        self, script_gen_real_issues: Any, path: str, expected: bool
    ) -> None:
        """Test-path detection holds across all documented heuristics and returns a strict bool.

        The caller compares with ``is True`` and stores the result in JSON, so a truthy or falsy non-bool is a defect; a
        conftest.py anywhere in the tree must be excluded from ground-truth source files.
        """
        assert script_gen_real_issues._is_test_path(path) is expected, f"_is_test_path({path!r}) expected {expected}"


class TestModuleFor:
    """Tests for module_for(path: str) -> str.

    Contract (docstring): strips leading ``src/``, collapses ``__init__.py`` to package name, drops ``.py``, replaces
    ``/`` with ``.``.
    """

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            # --- src/ prefix stripping ---
            pytest.param(
                TRAINER_FILE, "lightning.pytorch.trainer.trainer", id="src-lightning-pytorch-trainer-trainer.py"
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
            # --- non-root src/ segment: only a leading src/ is stripped, but every slash still becomes a dot ---
            pytest.param("pkg/src/module.py", "pkg.src.module", id="pkg-src-module.py"),
        ],
    )
    def test_converts_paths_to_dotted_module_names(self, script_gen_real_issues: Any, path: str, expected: str) -> None:
        """Dotted module names follow all documented path patterns and are always plain strings.

        A repo with a ``pkg/src/module.py`` layout keeps its inner ``src`` segment, since only a leading ``src/`` is
        stripped; downstream JSON serialization assumes the result is a ``str``.
        """
        result = script_gen_real_issues.module_for(path)

        assert result == expected
        assert isinstance(result, str)


class TestBuildPrompt:
    """Tests for build_prompt(title, body) -> str.

    Contract (docstring): returns title + blank line + truncated body; body truncated to PROMPT_BODY_LIMIT characters;
    whitespace stripped.
    """

    @pytest.mark.parametrize(
        ("title", "body", "expected"),
        [
            pytest.param("Fix the trainer", "Some body text.", "Fix the trainer\n\nSome body text.", id="basic"),
            pytest.param("Title", "Short description.", "Title\n\nShort description.", id="short-body-verbatim"),
            pytest.param("  Padded title  ", "body", "Padded title\n\nbody", id="strips-title-whitespace"),
            pytest.param("Title", "\n\n  Body text.  \n", "Title\n\nBody text.", id="strips-body-whitespace"),
        ],
    )
    def test_joins_stripped_title_and_body_with_one_blank_line(
        self, script_gen_real_issues: Any, title: str, body: str, expected: str
    ) -> None:
        """Title and body become two sections separated by exactly one blank line, whitespace stripped.

        Raw issue titles and bodies from the API may carry leading or trailing spaces and newlines; a short body is
        reproduced verbatim, neither truncated nor padded.
        """
        assert script_gen_real_issues.build_prompt(title, body) == expected

    @pytest.mark.parametrize(
        "excess",
        [pytest.param(500, id="over-limit"), pytest.param(0, id="exactly-at-limit")],
    )
    def test_truncates_body_to_the_limit_without_off_by_one(self, script_gen_real_issues: Any, excess: int) -> None:
        """A body over PROMPT_BODY_LIMIT keeps only its first PROMPT_BODY_LIMIT characters; one at the limit is whole.

        The boundary value guards against an off-by-one: a body exactly at the limit must be reproduced unchanged.
        """
        limit = script_gen_real_issues.PROMPT_BODY_LIMIT

        result = script_gen_real_issues.build_prompt("T", "a" * (limit + excess))

        assert result.split("\n\n", 1)[1] == "a" * limit


class TestIsMeaningfulIssue:
    """Tests for is_meaningful_issue(title, body) -> bool.

    Contract (docstring): returns True only when title is non-empty, not in GENERIC_TITLES, and body is non-trivial
    (truthy and non-blank).
    """

    @pytest.mark.parametrize(
        ("title", "body", "expected"),
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
            # --- generic titles are case-folded and stripped before the GENERIC_TITLES membership check;
            # the membership check itself over the full current set is proven exhaustively by
            # test_rejects_every_generic_title below, so only case-folding and padding are spot-checked here ---
            pytest.param("Bug", "Detailed description.", False, id="generic-title-mixed-case"),  # case folded
            pytest.param("BUG", "Detailed description.", False, id="generic-title-uppercase"),
            pytest.param("  bug  ", "Detailed body.", False, id="generic-title-padded"),  # API may pad the title
            # --- empty / blank title ---
            pytest.param("", "Body here.", False, id="empty"),
            pytest.param("   ", "Body here.", False, id="whitespace"),  # whitespace-only title
            # --- missing / blank body ---
            pytest.param("Specific title", None, False, id="specific-title-none"),
            pytest.param("Specific title", "", False, id="specific-title-empty"),
            pytest.param("Specific title", "   ", False, id="specific-title-whitespace"),  # whitespace-only body
        ],
    )
    def test_accepts_specific_issues_and_rejects_generic_or_blank_ones(
        self, script_gen_real_issues: Any, title: str, body: str | None, expected: bool
    ) -> None:
        """Specific titles with a real body are accepted; generic, blank, or body-less issues are filtered out.

        Scenario: the API may return padded or oddly-cased titles and missing bodies, and none of those may slip a
        low-signal issue into the task set.
        """
        assert script_gen_real_issues.is_meaningful_issue(title, body) is expected, (
            f"is_meaningful_issue({title!r}, {body!r}) expected {expected}"
        )

    @pytest.mark.parametrize("generic", GENERIC_TITLES)
    def test_rejects_every_generic_title(self, script_gen_real_issues: Any, generic: str) -> None:
        """Every entry in GENERIC_TITLES is rejected even with a valid body.

        Scenario: an added generic title must make the explicit parameter set fail closed until its rejection case is
        added.
        """
        body = "Non-trivial body that would normally qualify."

        assert set(GENERIC_TITLES) == script_gen_real_issues.GENERIC_TITLES
        assert script_gen_real_issues.is_meaningful_issue(generic, body) is False, (
            f"Generic title {generic!r} should be rejected"
        )


class TestBuildTask:
    """Tests for build_task(index, record) -> dict.

    Contract (docstring + schema): returns a task dict with keys id, type, source, workflow_subtype, difficulty,
    issue_number, issue_url, pr_number, pr_url, prompt, ground_truth, primary_module, scoreable, pr_closes_issue.
    """

    _REQUIRED_KEYS: ClassVar = {
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

    def test_contains_all_required_keys(self, default_task: dict[str, Any]) -> None:
        """Every documented schema key is present in the output dict.

        Scenario: the downstream harness reads each key directly; a missing key is a KeyError at scoring time.
        """
        missing = self._REQUIRED_KEYS - default_task.keys()

        assert not missing, f"Missing keys in task dict: {missing}"

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            pytest.param("type", "real_issue", id="type"),
            pytest.param("source", "real_issue", id="source"),
            pytest.param("scoreable", True, id="scoreable"),
            pytest.param("workflow_subtype", "pre_implementation_research", id="workflow-subtype"),
            pytest.param("primary_module_basis", "first_file", id="primary-module-basis-first-file"),
        ],
    )
    def test_fixed_fields_keep_documented_values(self, default_task: dict[str, Any], field: str, expected: Any) -> None:
        """Provenance and routing fields keep their documented fixed values for a record without change-size data.

        Scenario: the harness picks the scorer from type/source and the workflow from workflow_subtype, so another value
        routes a task wrongly; every built task has ground truth, so scoreable is True. A hand-authored PR carries no
        additions/deletions, so primary_module falls back to the first file and the basis flags that as 'first_file'.
        """
        assert default_task[field] == expected

    @pytest.mark.parametrize(
        ("index", "expected_id"),
        [
            pytest.param(1, "OSS-01", id="1"),
            pytest.param(9, "OSS-09", id="9"),
            pytest.param(10, "OSS-10", id="10"),
            pytest.param(20, "OSS-20", id="20"),
        ],
    )
    def test_id_uses_a_zero_padded_two_digit_ordinal(
        self, script_gen_real_issues: Any, index: int, expected_id: str
    ) -> None:
        """The task id is ``OSS-`` followed by the 1-based ordinal padded to two digits.

        Scenario: ids sort and compare as strings downstream, so single-digit ordinals must keep their leading zero.
        """
        task = script_gen_real_issues.build_task(index, _make_record(script_gen_real_issues))

        assert task["id"] == expected_id

    def test_ground_truth_lists_files_and_their_count(self, script_gen_real_issues: Any) -> None:
        """Ground truth contains the files_changed list and the file_count integer.

        Scenario: the scoring harness reads both sub-keys directly, so they must agree with the PR's source files.
        """
        source_files = [TRAINER_FILE, FIT_LOOP_FILE]
        pr = _make_pr(script_gen_real_issues, source_files=source_files)
        record = _make_record(script_gen_real_issues, pr=pr)

        task = script_gen_real_issues.build_task(1, record)

        assert task["ground_truth"]["files_changed"] == source_files
        assert task["ground_truth"]["file_count"] == 2

    @pytest.mark.parametrize(
        ("source_files", "expected_difficulty"),
        [
            pytest.param(["src/a.py"], "simple", id="one-file"),
            pytest.param(["src/a.py", "src/b.py"], "medium", id="two-files"),
            pytest.param(["src/a.py", "src/b.py", "src/c.py"], "medium", id="three-files"),
            pytest.param(["src/a.py", "src/b.py", "src/c.py", "src/d.py"], "hard", id="four-files"),
        ],
    )
    def test_difficulty_derives_from_source_file_count(
        self, script_gen_real_issues: Any, source_files: list[str], expected_difficulty: str
    ) -> None:
        """The difficulty label is derived from the source file count through difficulty_for.

        Scenario: the task dict carries the same band that difficulty_for documents for that many ground-truth files.
        """
        pr = _make_pr(script_gen_real_issues, source_files=source_files)

        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues, pr=pr))

        assert task["difficulty"] == expected_difficulty

    def test_primary_module_comes_from_the_first_source_file(self, script_gen_real_issues: Any) -> None:
        """Without change-size data primary_module is derived from the first source file via module_for.

        Scenario: the harness uses primary_module as an index key; it must reflect the first file in source_files.
        """
        pr = _make_pr(script_gen_real_issues, source_files=[TRAINER_FILE, FIT_LOOP_FILE])

        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues, pr=pr))

        assert task["primary_module"] == "lightning.pytorch.trainer.trainer"

    def test_primary_module_picks_the_most_changed_file(self, script_gen_real_issues: Any) -> None:
        """primary_module reflects the most-changed file, not the first one.

        Scenario: a multi-file PR whose second file has the larger diff must not yield the first file's module as
        primary — the bigger diff drives it.
        """
        changes = {TRAINER_FILE: 3, FIT_LOOP_FILE: 90}
        pr = _make_pr(script_gen_real_issues, source_files=[TRAINER_FILE, FIT_LOOP_FILE], source_changes=changes)

        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues, pr=pr))

        assert task["primary_module"] == "lightning.pytorch.loops.fit_loop"
        assert task["primary_module_basis"] == "most_changed"

    def test_issue_url_contains_the_issue_number(self, script_gen_real_issues: Any) -> None:
        """The issue_url encodes the issue number under the repo base URL.

        Scenario: task output links to the original issue; a wrong URL breaks the reviewer workflow.
        """
        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues, number=42))

        assert task["issue_url"] == f"{script_gen_real_issues.REPO_URL}/issues/42"
        assert task["issue_number"] == 42

    def test_pr_url_contains_the_pr_number(self, script_gen_real_issues: Any) -> None:
        """The pr_url encodes the PR number under the repo base URL.

        Scenario: the reviewer follows pr_url to inspect the fix; a wrong URL breaks that workflow.
        """
        pr = _make_pr(script_gen_real_issues, number=777)

        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues, pr=pr))

        assert task["pr_url"] == f"{script_gen_real_issues.REPO_URL}/pull/777"
        assert task["pr_number"] == 777

    @pytest.mark.parametrize("closes_issue", [True, False])
    def test_pr_closes_issue_is_propagated(self, script_gen_real_issues: Any, closes_issue: bool) -> None:
        """pr_closes_issue reflects PullRequestInfo.closes_issue exactly.

        Scenario: the downstream quality audit checks this flag; a wrong value masks the provenance quality of the task.
        """
        pr = _make_pr(script_gen_real_issues, closes_issue=closes_issue)

        task = script_gen_real_issues.build_task(1, _make_record(script_gen_real_issues, pr=pr))

        assert task["pr_closes_issue"] is closes_issue

    def test_prompt_starts_with_the_title_and_contains_the_body(self, script_gen_real_issues: Any) -> None:
        """The prompt field is built from the title and body via build_prompt.

        Scenario: the agent receives the prompt string; it must contain the issue title as the first line followed by
        the body.
        """
        record = _make_record(script_gen_real_issues, title="Trainer crashes on TPU", body="Reproducible on v2.0.")

        prompt: str = script_gen_real_issues.build_task(1, record)["prompt"]

        assert prompt.startswith("Trainer crashes on TPU")
        assert "Reproducible on v2.0." in prompt


class TestSelectPrimaryModule:
    """Tests for select_primary_module(source_files, source_changes) -> (module, basis)."""

    @pytest.mark.parametrize(
        ("source_files", "source_changes", "expected_module", "expected_basis"),
        [
            # most-changed wins even when it is not first
            pytest.param(
                ["src/a.py", "src/b.py"], {"src/a.py": 3, "src/b.py": 40}, "b", "most_changed", id="larger-diff-wins"
            ),
            # first-file tiebreak when change sizes are equal
            pytest.param(
                ["src/a.py", "src/b.py"], {"src/a.py": 10, "src/b.py": 10}, "a", "most_changed", id="equal-first-file"
            ),
            # no signal (None) -> first file
            pytest.param(["src/a.py", "src/b.py"], None, "a", "first_file", id="no-change-data"),
            # all-zero changes -> treated as no signal -> first file
            pytest.param(
                ["src/a.py", "src/b.py"], {"src/a.py": 0, "src/b.py": 0}, "a", "first_file", id="all-zero-changes"
            ),
            # single file -> that file regardless of basis
            pytest.param(["src/only.py"], {"src/only.py": 5}, "only", "most_changed", id="single-file"),
        ],
    )
    def test_selects_by_most_changed_with_first_file_fallback(
        self,
        script_gen_real_issues: Any,
        source_files: list[str],
        source_changes: dict[str, int] | None,
        expected_module: str,
        expected_basis: str,
    ) -> None:
        """Most-changed selection, first-file tiebreak, and no-signal fallback each report their basis.

        Scenario: ties and missing or all-zero change data carry no ranking signal, so the first file is chosen and,
        when the data is absent, the basis says so.
        """
        result = script_gen_real_issues.select_primary_module(source_files, source_changes)

        assert result == (expected_module, expected_basis)


class TestResolveMergedPrProvenance:
    """Tests for the weak-provenance warning emitted by resolve_merged_pr."""

    @pytest.mark.parametrize(
        ("closes_issue", "warns"),
        [
            pytest.param(False, True, id="non-closing-fallback-warns"),
            pytest.param(True, False, id="closing-candidate-silent"),
        ],
    )
    def test_warns_only_when_falling_back_to_a_non_closing_pr(
        self,
        script_gen_real_issues: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        closes_issue: bool,
        warns: bool,
    ) -> None:
        """A non-closing candidate emits a visible stderr warning; a closing one is chosen silently.

        Scenario: with no cross-referenced PR carrying a closes/fixes/resolves keyword, the weaker timeline-only
        linkage must not be accepted silently; when strong provenance exists the fallback path is never taken.
        """
        monkeypatch.setattr(
            script_gen_real_issues,
            "_inspect_pr",
            lambda number, min_py, max_py, issue_number=None: _make_pr(
                script_gen_real_issues, number=number, closes_issue=closes_issue
            ),
        )

        result = script_gen_real_issues.resolve_merged_pr([101], 1, 5, issue_number=42)

        err = capsys.readouterr().err
        assert result.number == 101
        assert result.closes_issue is closes_issue
        assert ("weak provenance" in err) is warns
        assert ("#42" in err) is warns


class TestInspectPrSourceFiltering:
    """Tests for _inspect_pr source-file filtering using mocked GitHub data."""

    def test_filters_test_files_and_records_source_change_sizes(
        self, script_gen_real_issues: Any, stub_gh_pr: Callable[[list[dict[str, Any]]], None]
    ) -> None:
        """Only non-test Python files become source ground truth; change sizes are retained.

        Scenario: a merged PR touches a source file, a test file and a Markdown file. Only the source file is ground
        truth, its size is additions plus deletions, and the closing keyword in the PR body is recognised.
        """
        stub_gh_pr(
            [
                {"path": "src/pkg/core.py", "additions": 3, "deletions": 2},
                {"path": "tests/test_core.py", "additions": 100, "deletions": 0},
                {"path": "docs/guide.md", "additions": 9, "deletions": 1},
            ]
        )

        result = script_gen_real_issues._inspect_pr(123, min_py=1, max_py=5, issue_number=42)

        assert result is not None
        assert result.source_files == ["src/pkg/core.py"]
        assert result.py_file_count == 2
        assert result.source_changes == {"src/pkg/core.py": 5}
        assert result.closes_issue is True

    @pytest.mark.parametrize(
        "files",
        [
            pytest.param(
                [
                    {"path": "tests/test_core.py", "additions": 10, "deletions": 0},
                    {"path": "src/pkg/core_test.py", "additions": 1, "deletions": 1},
                ],
                id="python-changes-only-in-tests",
            ),
            pytest.param([{"path": "README.md", "additions": 10, "deletions": 0}], id="no-python-files"),
        ],
    )
    def test_pr_without_python_source_returns_none(
        self,
        script_gen_real_issues: Any,
        stub_gh_pr: Callable[[list[dict[str, Any]]], None],
        files: list[dict[str, Any]],
    ) -> None:
        """A PR with Python changes only in tests, or without Python files, is not a source-backed task.

        Scenario: test-only Python changes and a PR without Python files cannot produce Python source ground truth.
        """
        stub_gh_pr(files)

        assert script_gen_real_issues._inspect_pr(123, min_py=1, max_py=5, issue_number=42) is None
