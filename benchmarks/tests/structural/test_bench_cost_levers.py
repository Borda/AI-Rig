"""Tests for the cost-lever layer of the codemap LLM benchmark runner.

Covers the four cost levers in run-claude-structural.py without any LLM call or
benchmark execution — every test drives the pure selection / provenance / aggregation
helpers directly, or writes synthetic JSONL result fixtures:

  - Resume/skip cache: provenance fingerprints (repo_sha / index_sha / task_hash), resume-key
    matching, cache load from prior JSONL, and cached-line reconstruction.
  - Profiles: dev-tagged subset selection, release full-matrix, and the None (unchanged) default.
  - Tiered protocol: haiku full / sonnet dev-subset / opus disagreement adjudication.
  - RI gating: real_issue tasks dropped unless release profile or explicit selection.
  - Self-consistency: index-derived-GT tasks excluded from headline accuracy aggregates and
    reported in a separate row.

No claude subprocess is launched by any test in this module.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

SUITE = Path(__file__).resolve().parent.parent.parent / "suites" / "tasks-bench.json"


@pytest.fixture(name="suite_tasks", scope="module")
def _suite_tasks() -> list[dict]:
    """Load the shipped benchmark suite once for read-only tag checks."""
    return json.loads(SUITE.read_text())["tasks"]


# ---------------------------------------------------------------------------
# Helpers to build synthetic tasks / runs / result lines
# ---------------------------------------------------------------------------


def _task(task_id: str, task_type: str = "symbol_extraction", **extra: Any) -> dict:
    """Build a task for selection tests, allowing explicit fields to override defaults.

    >>> _task("example", "review", prompt="Inspect the module")
    {'id': 'example', 'type': 'review', 'prompt': 'Inspect the module'}
    """
    return {"id": task_id, "type": task_type, "prompt": f"prompt for {task_id}", **extra}


def _result_line(
    task_id: str,
    arm: str,
    model: str,
    *,
    repo_sha: str = "repo-sha-fixture",
    index_sha: str = "i0",
    task_hash: str = "t0",
    correct: bool = True,
    scored: bool = True,
) -> dict:
    """Build a successful transport result with independently configurable scoring and provenance.

    >>> row = _result_line("example", "A_plain", "fixture-model", correct=False, task_hash="changed")
    >>> row["success"], row["task_hash"], row["quality"]
    (True, 'changed', {'scored': True, 'correct': False, 'extraction_failed': False})
    """
    return {
        "task_id": task_id,
        "arm": arm,
        "model": model,
        "success": True,
        "repo_sha": repo_sha,
        "index_sha": index_sha,
        "task_hash": task_hash,
        "incomplete": False,
        "quality": {"scored": scored, "correct": correct, "extraction_failed": False},
    }


def _write_jsonl(results_dir: Path, name: str, lines: list[dict]) -> Path:
    """Create the result directory and write one JSON object per line, replacing an existing file.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = _write_jsonl(Path(directory) / "results", "run.jsonl", [{"id": 1}, {"id": 2}])
    ...     [json.loads(line) for line in path.read_text().splitlines()]
    [{'id': 1}, {'id': 2}]
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / name
    with path.open("w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


# ===========================================================================
# Provenance fingerprints (Task 1)
# ===========================================================================


class TestProvenanceFingerprints:
    """repo_sha / index_sha / task_hash identify a (tree, index, task) for resume matching."""

    def test_repo_sha_unknown_for_non_repo(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A path that is not a git work tree yields the sentinel 'unknown'.

        An empty temporary directory has no ``.git`` above it, so asking for its HEAD degrades to the sentinel instead
        of raising.
        """
        assert script_run_bench._repo_sha(tmp_path) == "unknown"

    def test_repo_sha_reads_head(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A real git repo returns its 40-char HEAD SHA.

        Kept on a real throwaway repository: reading HEAD through git is the behaviour under test.
        """
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "f.txt").write_text("x")
        for argv in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t.t"],
            ["git", "config", "user.name", "t"],
            ["git", "add", "f.txt"],
            ["git", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(argv, cwd=repo, check=True, capture_output=True)
        sha = script_run_bench._repo_sha(repo)
        assert len(sha) == 40
        assert sha != "unknown"

    def test_index_sha_missing_file_is_unknown(self, script_run_bench: Any) -> None:
        """A missing index file degrades to 'unknown' rather than raising."""
        assert script_run_bench._index_sha(Path("/no/such/index.json")) == "unknown"

    @pytest.mark.parametrize(
        ("index_a", "index_b", "same"),
        [
            pytest.param(
                {"scan_version": 5, "scanned_at": "2026-01-01", "git_sha": "abc", "project": "p", "scan_root": "/r"}
                | {"modules": [1, 2, 3]},
                {"scan_version": 5, "scanned_at": "2026-01-01", "git_sha": "abc", "project": "p", "scan_root": "/r"}
                | {"modules": [9, 9, 9, 9]},
                True,
                id="body-change-keeps-fingerprint",
            ),
            pytest.param(
                {"scan_version": 5, "scanned_at": "t1"},
                {"scan_version": 5, "scanned_at": "t2"},
                False,
                id="rebuild-changes-fingerprint",
            ),
        ],
    )
    def test_index_sha_follows_head_meta_only(
        self, script_run_bench: Any, tmp_path: Path, index_a: dict, index_b: dict, same: bool
    ) -> None:
        """Only head-meta fields feed the index fingerprint; the modules body is ignored.

        Two indexes differing only in their modules body fingerprint equal, while a different ``scanned_at`` (a rebuild)
        changes it, so a resumed run never trusts results from a rebuilt index.
        """
        idx_a = tmp_path / "a.json"
        idx_b = tmp_path / "b.json"
        idx_a.write_text(json.dumps(index_a))
        idx_b.write_text(json.dumps(index_b))

        assert (script_run_bench._index_sha(idx_a) == script_run_bench._index_sha(idx_b)) is same

    @pytest.mark.parametrize(
        ("task_a", "task_b", "same"),
        [
            pytest.param({"id": "X", "p": "q"}, {"p": "q", "id": "X"}, True, id="key-order-invariant"),
            pytest.param({"id": "X", "prompt": "a"}, {"id": "X", "prompt": "b"}, False, id="content-change-differs"),
        ],
    )
    def test_task_hash_is_canonical_over_content(
        self, script_run_bench: Any, task_a: dict, task_b: dict, same: bool
    ) -> None:
        """Task hash ignores key order but changes with any content edit.

        Canonical JSON keeps reordered keys stable, while an edited prompt changes the hash and invalidates a stale
        resume match.
        """
        assert (script_run_bench._task_hash(task_a) == script_run_bench._task_hash(task_b)) is same


# ===========================================================================
# Resume cache (Task 1)
# ===========================================================================


class TestResumeCache:
    """Prior result lines are indexed by the six-field resume key and reused on ``--resume``."""

    def test_resume_key_orders_six_fields(self, script_run_bench: Any) -> None:
        """The resume key is the six identifying fields in a fixed order."""
        key = script_run_bench._resume_key(
            {"task_id": "SE-01", "arm": "plain", "model": "haiku", "repo_sha": "a", "index_sha": "b", "task_hash": "c"}
        )
        assert key == ("SE-01", "plain", "haiku", "a", "b", "c")

    def test_load_resume_cache_indexes_lines(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Every prior bench-*.jsonl line is indexed by its resume key."""
        results = tmp_path / "results"
        _write_jsonl(results, "bench-haiku-1.jsonl", [_result_line("SE-01", "plain", "haiku")])
        cache = script_run_bench._load_resume_cache(results)
        key = ("SE-01", "plain", "haiku", "repo-sha-fixture", "i0", "t0")
        assert key in cache

    def test_load_resume_cache_missing_dir_is_empty(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A missing results dir yields an empty cache, not an error."""
        assert script_run_bench._load_resume_cache(tmp_path / "nope") == {}

    def test_load_resume_cache_skips_malformed_lines(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Malformed JSONL lines are skipped; valid ones still load."""
        results = tmp_path / "results"
        results.mkdir()
        (results / "bench-haiku-x.jsonl").write_text("not json\n" + json.dumps(_result_line("SE-01", "plain", "haiku")))
        cache = script_run_bench._load_resume_cache(results)
        assert ("SE-01", "plain", "haiku", "repo-sha-fixture", "i0", "t0") in cache

    def test_later_file_wins_on_key_collision(self, script_run_bench: Any, tmp_path: Path) -> None:
        """When two files hold the same key, the lexically-later file's line wins."""
        results = tmp_path / "results"
        _write_jsonl(results, "bench-haiku-1.jsonl", [_result_line("SE-01", "plain", "haiku", correct=False)])
        _write_jsonl(results, "bench-haiku-2.jsonl", [_result_line("SE-01", "plain", "haiku", correct=True)])
        cache = script_run_bench._load_resume_cache(results)
        assert cache[("SE-01", "plain", "haiku", "repo-sha-fixture", "i0", "t0")]["quality"]["correct"] is True

    def test_run_from_cached_marks_resumed(self, script_run_bench: Any) -> None:
        """A reconstructed run copies known fields, rebuilds quality, and flags resumed."""
        line = _result_line("SE-01", "plain", "haiku", correct=True)
        line["task_type"] = "symbol_extraction"
        run = script_run_bench._run_from_cached(line)
        assert run.resumed is True
        assert run.quality.correct is True
        assert run.task_id == "SE-01"

    def test_run_from_cached_ignores_unknown_keys(self, script_run_bench: Any) -> None:
        """Unknown schema keys in a cached line are dropped, not passed to the constructor."""
        line = _result_line("SE-01", "plain", "haiku")
        line["task_type"] = "symbol_extraction"
        line["some_future_field"] = 123
        run = script_run_bench._run_from_cached(line)
        assert run.task_id == "SE-01"
        assert not hasattr(run, "some_future_field")


class TestRunnerResume:
    """BenchRunner short-circuits execution when a resume cache holds a matching tuple."""

    def _runner(self, script_run_bench: Any, tmp_path: Path, cache: dict) -> Any:
        """Build a BenchRunner with a pinned resume cache and fixed provenance."""
        runner = script_run_bench.BenchRunner(
            model_short="haiku",
            model_id="claude-haiku-4-5-20251001",
            repo_path=tmp_path,
            index_path=tmp_path / "idx.json",
            timeout=10,
            resume_cache=cache,
        )
        # Pin provenance so the resume key is deterministic without a real repo/index.
        runner.repo_sha = "repo-sha-fixture"
        runner.index_sha = "i0"
        return runner

    def test_resume_hit_skips_execution(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A matching cache entry is reused (resumed=True) and _execute is never called.

        The cache holds a correct result under the exact six-field key; the real execution path is replaced by a mock
        that fails the test if reached, which would mean a paid model call on an already-answered cell.
        """
        task = _task("SE-01")
        task_hash = script_run_bench._task_hash(task)
        key = ("SE-01", "plain", "haiku", "repo-sha-fixture", "i0", task_hash)
        cached = _result_line("SE-01", "plain", "haiku", task_hash=task_hash, correct=True)
        cached["task_type"] = "symbol_extraction"
        runner = self._runner(script_run_bench, tmp_path, {key: cached})
        execute = mock.Mock(side_effect=AssertionError("_execute must not run on a resume hit"))
        monkeypatch.setattr(runner, "_execute", execute)

        run = runner.run(task, "plain")

        execute.assert_not_called()
        assert run.resumed is True
        assert run.quality.correct is True
        assert run.repo_sha == "repo-sha-fixture"
        assert run.index_sha == "i0"
        assert run.task_hash == task_hash

    def test_resume_miss_executes_and_stamps_provenance(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cache miss runs _execute, then stamps provenance + self_consistency on the result.

        With an empty cache the runner must execute the cell (stubbed at the model boundary) and copy the repo and index
        fingerprints, task hash and the task's self-consistency flag onto the returned run.
        """
        task = _task("CQ-02", task_type="code_quality", self_consistency=True)
        sentinel = script_run_bench.BenchRun(
            arm="plain", task_id="CQ-02", task_type="code_quality", model="haiku", success=True
        )
        runner = self._runner(script_run_bench, tmp_path, {})  # empty cache → always miss
        monkeypatch.setattr(runner, "_execute", mock.Mock(return_value=sentinel))

        run = runner.run(task, "plain")

        assert run.resumed is False
        assert run.self_consistency is True
        assert run.repo_sha == "repo-sha-fixture"
        assert run.index_sha == "i0"
        assert run.task_hash == script_run_bench._task_hash(task)

    def test_no_cache_never_resumes(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Execute every run while retaining provenance when no resume cache is configured.

        A runner built without a ``resume_cache`` argument must call the (stubbed) model boundary and still stamp the
        task hash, so later runs can resume from its output.
        """
        task = _task("SE-01")
        sentinel = script_run_bench.BenchRun(
            arm="plain", task_id="SE-01", task_type="symbol_extraction", model="haiku", success=True
        )
        runner = script_run_bench.BenchRunner(
            model_short="haiku",
            model_id="m",
            repo_path=tmp_path,
            index_path=tmp_path / "idx.json",
            timeout=10,
        )
        runner.repo_sha, runner.index_sha = "repo-sha-fixture", "i0"
        monkeypatch.setattr(runner, "_execute", mock.Mock(return_value=sentinel))

        run = runner.run(task, "plain")

        assert run.resumed is False
        assert run.task_hash == script_run_bench._task_hash(task)


# ===========================================================================
# Profiles (Task 2)
# ===========================================================================


class TestProfileSelection:
    """Dev keeps the dev-tagged subset; release keeps everything; None is unchanged."""

    @pytest.fixture(name="mixed_tasks")
    def _mixed_tasks(self) -> list[dict]:
        """Provide development-tagged, untagged, and real-issue tasks for profile selection."""
        return [
            _task("SE-01", profiles=["dev"]),
            _task("SE-02"),
            _task("FN-01", "fn_call_graph", profiles=["dev"]),
            _task("RI-01", "real_issue"),
        ]

    def test_dev_keeps_only_tagged(self, script_run_bench: Any, mixed_tasks: list[dict]) -> None:
        """The dev profile filters to tasks tagged profiles=['dev']."""
        got = [t["id"] for t in script_run_bench._apply_profile(mixed_tasks, "dev")]
        assert got == ["SE-01", "FN-01"]

    def test_release_keeps_all(self, script_run_bench: Any, mixed_tasks: list[dict]) -> None:
        """The release profile keeps every task (RI gated separately)."""
        got = {t["id"] for t in script_run_bench._apply_profile(mixed_tasks, "release")}
        assert got == {"SE-01", "SE-02", "FN-01", "RI-01"}

    def test_none_is_unchanged(self, script_run_bench: Any, mixed_tasks: list[dict]) -> None:
        """No profile leaves the task list untouched (zero behavior change)."""
        got = [t["id"] for t in script_run_bench._apply_profile(mixed_tasks, None)]
        assert got == [t["id"] for t in mixed_tasks]

    @pytest.mark.parametrize(
        ("task", "is_dev"),
        [
            pytest.param(_task("X", profiles=["dev"]), True, id="dev-tagged"),
            pytest.param(_task("Y"), False, id="untagged"),
        ],
    )
    def test_is_dev_task_reads_tag(self, script_run_bench: Any, task: dict, is_dev: bool) -> None:
        """_is_dev_task is true only when 'dev' is in the profiles tag.

        A task tagged ``profiles=["dev"]`` is a dev task; a task with no profiles key is not.
        """
        assert script_run_bench._is_dev_task(task) is is_dev


class TestDevSubsetInSuite:
    """The shipped tasks-bench.json declares a valid stratified dev subset."""

    def test_dev_subset_size(self, suite_tasks: list[dict]) -> None:
        """About 12 tasks carry the dev tag, keeping the regression subset fast.

        The shipped suite is read as-is, so adding or removing dev tags beyond the 10-14 band shows up here.
        """
        dev = [t for t in suite_tasks if "dev" in (t.get("profiles") or [])]
        assert 10 <= len(dev) <= 14

    def test_dev_subset_covers_required_series(self, suite_tasks: list[dict]) -> None:
        """Dev subset has at least one task per SE/FN/RV/CQ/BR/DG/FT + DI/GR series, and excludes RI.

        A stratified subset must still sample every task family except the expensive real-issue one.
        """
        dev_series = {t["id"].split("-")[0] for t in suite_tasks if "dev" in (t.get("profiles") or [])}
        assert {"SE", "FN", "RV", "CQ", "BR", "DG", "FT", "DI", "GR"} <= dev_series
        assert "RI" not in dev_series

    def test_dev_subset_avoids_self_consistency_tasks(self, suite_tasks: list[dict]) -> None:
        """Dev regression signal excludes index-derived-GT tasks, keeping accuracy clean.

        No task may be both dev-tagged and self-consistency-tagged.
        """
        dev = {t["id"] for t in suite_tasks if "dev" in (t.get("profiles") or [])}
        sc = {t["id"] for t in suite_tasks if t.get("self_consistency")}
        assert not (dev & sc)


# ===========================================================================
# RI gating (Task 4)
# ===========================================================================


class TestRiGating:
    """real_issue tasks run only under release profile or explicit selection."""

    @pytest.fixture(name="tasks")
    def _tasks(self) -> list[dict]:
        """Provide the task collection used by this selection or coverage scenario."""
        return [_task("RI-01", "real_issue"), _task("SE-01")]

    @pytest.mark.parametrize(
        "profile",
        [
            pytest.param(None, id="no-profile"),
            pytest.param("dev", id="dev-profile"),
        ],
    )
    def test_ri_dropped_without_release_or_explicit_selection(
        self, script_run_bench: Any, tasks: list[dict], profile: str | None
    ) -> None:
        """RI is dropped (2M-token outlier) unless the release profile or an explicit selection opts it in.

        Scenario: no profile and no explicit selection drops RI; the dev profile does NOT opt RI in, only release does.
        """
        got = [t["id"] for t in script_run_bench._gate_ri(tasks, profile, explicit=False)]
        assert got == ["SE-01"]

    @pytest.mark.parametrize(
        ("profile", "explicit"),
        [
            pytest.param("release", False, id="release-profile"),
            pytest.param(None, True, id="explicit-selection"),
        ],
    )
    def test_ri_kept_under_release_or_explicit_selection(
        self, script_run_bench: Any, tasks: list[dict], profile: str | None, explicit: bool
    ) -> None:
        """The release profile or an explicit ``--tasks``/``--task-type`` selection opts RI back in."""
        got = {t["id"] for t in script_run_bench._gate_ri(tasks, profile, explicit=explicit)}
        assert got == {"RI-01", "SE-01"}


class TestSelectTasksIntegration:
    """_select_tasks composes base selection + profile + RI gating + tiered."""

    def _selection(self, script_run_bench: Any, **overrides: Any) -> Any:
        """Construct selection arguments with scenario overrides over a complete baseline."""
        base = {
            "all_tasks": [_task("SE-01", profiles=["dev"]), _task("SE-02"), _task("RI-01", "real_issue")],
            "ids": None,
            "task_type": None,
            "run_all": True,
            "external_ids": set(),
            "patch_ids": set(),
            "profile": None,
            "tiered": False,
            "model": "haiku",
        }
        base.update(overrides)
        return script_run_bench.TaskSelection(**base)

    @pytest.mark.parametrize(
        ("overrides", "expected_ids"),
        [
            pytest.param({}, {"SE-01", "SE-02"}, id="run-all-drops-ri"),
            pytest.param({"profile": "release"}, {"SE-01", "SE-02", "RI-01"}, id="release-profile-keeps-ri"),
            pytest.param({"profile": "dev"}, {"SE-01"}, id="dev-profile-selects-tagged-subset"),
            pytest.param({"run_all": False, "ids": {"RI-01"}}, {"RI-01"}, id="explicit-ids-keep-ri"),
        ],
    )
    def test_selector_and_profile_compose_task_selection(
        self, script_run_bench: Any, tmp_path: Path, overrides: dict[str, Any], expected_ids: set[str]
    ) -> None:
        """Verify command-line option behavior.

        ``--all`` with no profile drops RI (gated) but keeps the rest; ``--profile release`` keeps RI in the full
        matrix; ``--profile dev`` narrows to the dev-tagged subset; explicit ``--tasks`` selection opts RI back in even
        without release.
        """
        sel = self._selection(script_run_bench, **overrides)
        got = {t["id"] for t in script_run_bench._select_tasks(sel, tmp_path, "repo-sha-fixture", "i0")}
        assert got == expected_ids

    def test_no_selector_returns_none(self, script_run_bench: Any, tmp_path: Path) -> None:
        """No selector (no ``--tasks``/``--type``/``--all``/subset) returns None so main() can error."""
        sel = self._selection(script_run_bench, run_all=False)
        assert script_run_bench._select_tasks(sel, tmp_path, "repo-sha-fixture", "i0") is None


# ===========================================================================
# Tiered protocol (Task 3)
# ===========================================================================


class TestTieredSelection:
    """Select model-specific task subsets and disagreement cases."""

    @pytest.fixture(name="tasks")
    def _tasks(self) -> list[dict]:
        """Provide the task collection used by this selection or coverage scenario."""
        return [_task("SE-01", profiles=["dev"]), _task("SE-02", profiles=["dev"]), _task("FN-01", "fn_call_graph")]

    @pytest.mark.parametrize(
        ("model", "expected_ids"),
        [
            pytest.param("haiku", {"SE-01", "SE-02", "FN-01"}, id="haiku-runs-every-candidate"),
            pytest.param("sonnet", {"SE-01", "SE-02"}, id="sonnet-runs-dev-subset"),
        ],
    )
    def test_model_tier_selects_its_task_subset(
        self, script_run_bench: Any, tasks: list[dict], tmp_path: Path, model: str, expected_ids: set[str]
    ) -> None:
        """The haiku tier runs every candidate task; the sonnet tier runs only the dev-tagged subset."""
        got = {t["id"] for t in script_run_bench._tiered_tasks(tasks, model, tmp_path, "repo-sha-fixture", "i0")}
        assert got == expected_ids

    def test_opus_tier_selects_disagreements(self, script_run_bench: Any, tasks: list[dict], tmp_path: Path) -> None:
        """The opus tier runs only tasks where haiku and sonnet verdicts disagree."""
        results = tmp_path / "results"
        # SE-01: haiku correct, sonnet wrong → disagree. SE-02: both correct → agree.
        _write_jsonl(
            results,
            "bench-haiku-1.jsonl",
            [
                _result_line("SE-01", "plain", "haiku", correct=True),
                _result_line("SE-02", "plain", "haiku", correct=True),
            ],
        )
        _write_jsonl(
            results,
            "bench-sonnet-1.jsonl",
            [
                _result_line("SE-01", "plain", "sonnet", correct=False),
                _result_line("SE-02", "plain", "sonnet", correct=True),
            ],
        )
        got = {t["id"] for t in script_run_bench._tiered_tasks(tasks, "opus", results, "repo-sha-fixture", "i0")}
        assert got == {"SE-01"}

    def test_opus_tier_empty_without_prior_results(
        self, script_run_bench: Any, tasks: list[dict], tmp_path: Path
    ) -> None:
        """With no prior-tier results, opus adjudication selects nothing."""
        got = script_run_bench._tiered_tasks(tasks, "opus", tmp_path, "repo-sha-fixture", "i0")
        assert got == []

    def test_opus_tier_respects_provenance(self, script_run_bench: Any, tasks: list[dict], tmp_path: Path) -> None:
        """Prior-tier lines from a different repo_sha are ignored (no false disagreement)."""
        results = tmp_path / "results"
        _write_jsonl(results, "bench-haiku-1.jsonl", [_result_line("SE-01", "plain", "haiku", repo_sha="OTHER")])
        _write_jsonl(results, "bench-sonnet-1.jsonl", [_result_line("SE-01", "plain", "sonnet", correct=False)])
        got = script_run_bench._tiered_tasks(tasks, "opus", results, "repo-sha-fixture", "i0")
        assert got == []


class TestCorrectByTask:
    """_correct_by_task folds a tier's arms to one verdict per task, provenance-filtered."""

    def test_conjunctive_over_arms(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A task is 'correct' for the tier only when every scored arm was correct."""
        results = tmp_path / "results"
        _write_jsonl(
            results,
            "bench-haiku-1.jsonl",
            [
                _result_line("SE-01", "plain", "haiku", correct=True),
                _result_line("SE-01", "codemap", "haiku", correct=False),
            ],
        )
        got = script_run_bench._correct_by_task(results, "haiku", "repo-sha-fixture", "i0")
        assert got == {"SE-01": False}

    def test_filters_by_model_and_provenance(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Lines from another model or index_sha are excluded."""
        results = tmp_path / "results"
        _write_jsonl(
            results,
            "bench-x.jsonl",
            [
                _result_line("SE-01", "plain", "sonnet", correct=True),  # wrong model
                _result_line("SE-02", "plain", "haiku", index_sha="OTHER", correct=True),  # wrong index
                _result_line("SE-03", "plain", "haiku", correct=True),  # kept
            ],
        )
        got = script_run_bench._correct_by_task(results, "haiku", "repo-sha-fixture", "i0")
        assert got == {"SE-03": True}

    def test_unscored_lines_excluded(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Lines with scored=False don't contribute a verdict."""
        results = tmp_path / "results"
        _write_jsonl(results, "bench-h.jsonl", [_result_line("SE-01", "plain", "haiku", scored=False)])
        assert script_run_bench._correct_by_task(results, "haiku", "repo-sha-fixture", "i0") == {}


# ===========================================================================
# Self-consistency exclusion (Task 5)
# ===========================================================================


def _run(script_run_bench: Any, task_id: str, arm: str, *, correct: bool, self_consistency: bool) -> Any:
    """Build a scored run whose correctness and self-consistency eligibility vary independently.

    >>> run = _run(getfixture("script_run_bench"), "example", "plain", correct=False, self_consistency=True)
    >>> run.quality.scored, run.quality.correct, run.self_consistency
    (True, False, True)
    """
    run = script_run_bench.BenchRun(arm=arm, task_id=task_id, task_type="code_quality", model="haiku", success=True)
    run.quality = script_run_bench.BenchQuality(scored=True, correct=correct)
    run.self_consistency = self_consistency
    return run


class TestSelfConsistencyExclusion:
    """Index-derived-GT runs are excluded from headline accuracy aggregates."""

    def test_is_self_consistency_reads_flag(self, script_run_bench: Any) -> None:
        """_is_self_consistency reflects the run flag; None is False."""
        assert script_run_bench._is_self_consistency(None) is False
        run = _run(script_run_bench, "CQ-02", "codemap", correct=True, self_consistency=True)
        assert script_run_bench._is_self_consistency(run) is True

    def test_paired_accuracy_excludes_self_consistency(self, script_run_bench: Any) -> None:
        """A self-consistency task is not counted in the paired-accuracy denominator."""
        runs = [
            _run(script_run_bench, "SE-01", "plain", correct=True, self_consistency=False),
            _run(script_run_bench, "SE-01", "codemap", correct=True, self_consistency=False),
            _run(script_run_bench, "CQ-02", "plain", correct=True, self_consistency=True),
            _run(script_run_bench, "CQ-02", "codemap", correct=True, self_consistency=True),
        ]
        paired = script_run_bench._paired_accuracy(runs)
        assert paired is not None
        assert paired["n"] == 1  # only SE-01 counted; CQ-02 excluded

    def test_paired_accuracy_none_when_only_self_consistency(self, script_run_bench: Any) -> None:
        """When the only paired task is self-consistency, paired accuracy is None."""
        runs = [
            _run(script_run_bench, "CQ-02", "plain", correct=True, self_consistency=True),
            _run(script_run_bench, "CQ-02", "codemap", correct=True, self_consistency=True),
        ]
        assert script_run_bench._paired_accuracy(runs) is None

    def test_headline_accuracy_excludes_self_consistency(self, script_run_bench: Any, capsys: Any) -> None:
        """The per-arm headline accuracy denominator drops self-consistency runs."""
        runs = [
            _run(script_run_bench, "SE-01", "codemap", correct=True, self_consistency=False),
            _run(script_run_bench, "CQ-02", "codemap", correct=False, self_consistency=True),
        ]
        script_run_bench._print_summary(runs, "haiku")
        out = capsys.readouterr().out
        # codemap headline = 1/1 (SE-01 only); CQ-02 excluded from the scored denominator.
        assert "codemap accuracy = 100.0%  (1/1 scored)" in out

    def test_self_consistency_reported_separately(self, script_run_bench: Any, capsys: Any) -> None:
        """Self-consistency tasks surface in a dedicated row, not the headline."""
        runs = [_run(script_run_bench, "CQ-02", "codemap", correct=True, self_consistency=True)]
        script_run_bench._print_self_consistency(runs)
        out = capsys.readouterr().out
        assert "Self-consistency" in out
        assert "CQ-02" in out

    def test_self_consistency_row_absent_when_none(self, script_run_bench: Any, capsys: Any) -> None:
        """No self-consistency runs → no self-consistency row printed."""
        runs = [_run(script_run_bench, "SE-01", "codemap", correct=True, self_consistency=False)]
        script_run_bench._print_self_consistency(runs)
        assert "Self-consistency" not in capsys.readouterr().out


class TestSelfConsistencyInSuite:
    """The shipped suite tags tool-derived CQ tasks as self-consistency."""

    def test_tagged_tasks_are_tool_derived_cq_diagnostics(self, suite_tasks: list[dict]) -> None:
        """Coupled, xrefs, and combined tool-derived answers are demoted.

        Exactly CQ-03, CQ-04 and CQ-05 carry the self-consistency tag, because their ground truth comes from the tool
        under test.
        """
        sc = {t["id"] for t in suite_tasks if t.get("self_consistency")}
        assert sc == {"CQ-03", "CQ-04", "CQ-05"}

    def test_independent_cq_tasks_are_not_mislabelled_self_consistency(self, suite_tasks: list[dict]) -> None:
        """CQ-01 is independent; CQ-02 is approximate independent, not self-consistency.

        Neither of the two carries the tag, so their accuracy stays in the headline aggregate.
        """
        by_id = {t["id"]: t for t in suite_tasks}
        assert not by_id["CQ-01"].get("self_consistency")
        assert not by_id["CQ-02"].get("self_consistency")
