"""Contract tests for the gap rollup: counting by subject and gap identity, known limits, skipped ledgers."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gap_rollup
import pytest
from _hook_env import _symlinks_supported


def _record(run_id: str, gap_id: str, *, pr: int | None = 7, head: str = "a1b2c3d", **extra: Any) -> dict[str, Any]:
    """Build a valid gap record of run ``run_id`` on ``octo/demo``."""
    record: dict[str, Any] = {
        "schema": "gap-ledger",
        "schema_version": 1,
        "id": gap_id,
        "class": "process",
        "summary": f"{gap_id} observed",
        "owner": "workflow",
        "closable_by": "fix the workflow",
        "evidence": "review-report.md",
        "subject": {"repo": "octo/demo", "pr": pr, "head": head},
        "run": {
            "run_id": run_id,
            "kind": "review",
            "harness": "claude",
            "plugin": "oss",
            "plugin_version": "0.69.0",
            "route": "direct",
        },
    }
    record.update(extra)
    return record


_CUDA = {
    "class": "hardware-absent",
    "owner": "host",
    "hardware_class": "cuda",
    "reason": "This host has no NVIDIA GPU.",
    "host_class": {"os": "linux", "architecture": "x86_64"},
}


@pytest.fixture
def write_run(tmp_path: Path) -> Callable[[str, list[dict[str, Any]]], Path]:
    """Return a writer that stores one run's records as ``<tmp>/runs/<name>/gaps.jsonl``."""

    def _write(name: str, records: list[dict[str, Any]]) -> Path:
        path = tmp_path / "runs" / name / "gaps.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    return _write


def _rollup(tmp_path: Path, *limits: Path) -> dict[str, Any]:
    """Run the rollup over ``tmp_path`` and return its JSON document."""
    rollup = gap_rollup.build_rollup(gap_rollup.find_ledgers([tmp_path]), list(limits))
    return gap_rollup.to_json(rollup)


def _class_row(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the headline row of one class."""
    return next(row for row in data["classes"] if row["class"] == name)


class TestCounting:
    """Gaps count once per repository, subject and id, never per run directory."""

    def test_relative_and_absolute_roots_count_one_ledger(
        self, tmp_path: Path, write_run: Callable, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same ledger contributes once when found through a relative path and an absolute path."""
        ledger = write_run("run-001", [_record("run-001", "lint-not-run")])
        monkeypatch.chdir(tmp_path)

        paths = gap_rollup.find_ledgers([Path("runs"), ledger])
        assert gap_rollup.build_rollup(paths, []).ledgers == 1

    def test_same_gap_in_two_runs_of_one_pr_counts_once(self, tmp_path: Path, write_run: Callable) -> None:
        """A rerun that records the same gap on the same pull request adds a recording run, not a gap."""
        write_run("run-001", [_record("run-001", "codemap-index-stale")])
        write_run("run-002", [_record("run-002", "codemap-index-stale", carried_from="run-001")])
        row = _class_row(_rollup(tmp_path), "process")
        assert (row["distinct_gaps"], row["subjects"], row["runs"], row["carried"]) == (1, 1, 2, 1)

    def test_same_gap_on_two_prs_counts_twice(self, tmp_path: Path, write_run: Callable) -> None:
        """The subject is part of the identity: two pull requests are two gaps."""
        write_run("run-001", [_record("run-001", "codemap-index-stale")])
        write_run("run-002", [_record("run-002", "codemap-index-stale", pr=8)])
        assert _class_row(_rollup(tmp_path), "process")["distinct_gaps"] == 2

    def test_subject_without_pr_counts_by_head(self, tmp_path: Path, write_run: Callable) -> None:
        """A local run without a pull request is identified by its head commit."""
        write_run("run-001", [_record("run-001", "lint-not-run", pr=None, head="1111111")])
        write_run("run-002", [_record("run-002", "lint-not-run", pr=None, head="2222222")])
        assert _class_row(_rollup(tmp_path), "process")["subjects"] == 2

    def test_closing_event_retracts_the_gap(self, tmp_path: Path, write_run: Callable) -> None:
        """A later ``closed`` record of the same id in the same run removes the gap from every count."""
        write_run("run-001", [_record("run-001", "lint-not-run"), _record("run-001", "lint-not-run", status="closed")])
        data = _rollup(tmp_path)
        assert (data["distinct_gaps"], data["closed_in_run"]) == (0, 1)

    def test_class_conflict_counts_under_the_majority_class(self, tmp_path: Path, write_run: Callable) -> None:
        """A gap recorded under two classes counts once, under the class most runs used, and is listed."""
        write_run("run-001", [_record("run-001", "ci-red", **{"class": "project", "owner": "maintainer"})])
        write_run("run-002", [_record("run-002", "ci-red", **{"class": "project", "owner": "maintainer"})])
        write_run("run-003", [_record("run-003", "ci-red")])
        data = _rollup(tmp_path)
        assert (_class_row(data, "project")["distinct_gaps"], len(data["class_conflicts"])) == (1, 1)


class TestKnownLimits:
    """Known limits appear once per repository and never inflate per-PR counts."""

    @staticmethod
    def _limits(path: Path, repo: str = "octo/demo") -> Path:
        """Write a known-limits file listing the CUDA limit for ``repo`` and return its path."""
        entry = {
            "id": "cuda-absent",
            **{key: _CUDA[key] for key in ("class", "hardware_class", "reason", "host_class")},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema": "known-limits", "schema_version": 1, "repo": repo, "limits": [entry]}),
            encoding="utf-8",
        )
        return path

    def test_listed_limit_is_shown_once_across_prs(self, tmp_path: Path, write_run: Callable) -> None:
        """A CUDA limit recorded on two pull requests is one known-limit row and zero hardware gaps."""
        write_run("run-001", [_record("run-001", "cuda-absent", known_limit=True, **_CUDA)])
        write_run("run-002", [_record("run-002", "cuda-absent", pr=8, known_limit=True, **_CUDA)])
        data = _rollup(tmp_path, self._limits(tmp_path / "known-limits.json"))
        assert [(item["id"], item["host_class"], item["runs"]) for item in data["known_limits"]] == [
            ("cuda-absent", _CUDA["host_class"], 2)
        ]
        assert _class_row(data, "hardware-absent")["distinct_gaps"] == 0
        assert data["unverified_known_limits"] == []

    def test_limits_file_moves_an_unmarked_record_to_known_limits(self, tmp_path: Path, write_run: Callable) -> None:
        """A record matching the repository's known-limits file is shown as a known limit, not a gap."""
        write_run("run-001", [_record("run-001", "cuda-absent", **_CUDA)])
        data = _rollup(tmp_path, self._limits(tmp_path / "known-limits.json"))
        assert (len(data["known_limits"]), data["distinct_gaps"]) == (1, 0)

    @pytest.mark.parametrize(
        "registry_repo",
        [pytest.param(None, id="no-registry"), pytest.param("other/repo", id="registry-of-another-repository")],
    )
    def test_flag_without_a_registry_entry_counts_as_an_unverified_gap(
        self, tmp_path: Path, write_run: Callable, registry_repo: str | None
    ) -> None:
        """A ``known_limit`` flag no registry backs is an ordinary gap, also named as an unverified claim."""
        forged = {**_CUDA, "host_class": {"os": "plan9", "architecture": "vax"}}
        write_run("run-001", [_record("run-001", "cuda-absent", known_limit=True, **forged)])
        limits = [] if registry_repo is None else [self._limits(tmp_path / "limits.json", registry_repo)]
        data = _rollup(tmp_path, *limits)
        assert (data["known_limits"], data["distinct_gaps"]) == ([], 1)
        assert _class_row(data, "hardware-absent")["distinct_gaps"] == 1
        assert data["unverified_known_limits"] == [
            {"repo": "octo/demo", "id": "cuda-absent", "host_class": forged["host_class"], "runs": 1}
        ]
        assert data["routes"][0]["distinct_gaps"] == 1

    def test_registry_under_a_root_is_read_without_a_flag(
        self, tmp_path: Path, write_run: Callable, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The command line reads ``.notes/known-limits.json`` under a directory root by itself."""
        write_run("run-001", [_record("run-001", "cuda-absent", known_limit=True, **_CUDA)])
        self._limits(tmp_path / ".notes" / "known-limits.json")
        assert gap_rollup.main([str(tmp_path), "--format", "json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert (len(data["known_limits"]), data["unverified_known_limits"], data["distinct_gaps"]) == (1, [], 0)


class TestRepositoryIdentity:
    """One repository is one identity whatever case each emitter spelled it in."""

    def test_repository_case_does_not_split_a_gap(self, tmp_path: Path, write_run: Callable) -> None:
        """``Octo/Demo`` from a remote and ``octo/demo`` from a PR URL count the same pull-request gap once."""
        record = _record("run-001", "provisioning-failed-x", **{"class": "environment-provisionable"})
        write_run("run-001", [record])
        write_run(
            "run-002",
            [
                {
                    **record,
                    "subject": {**record["subject"], "repo": "Octo/Demo"},
                    "run": {**record["run"], "run_id": "run-002"},
                }
            ],
        )
        assert _rollup(tmp_path)["distinct_gaps"] == 1

    def test_registry_spelling_does_not_hide_a_limit(self, tmp_path: Path, write_run: Callable) -> None:
        """A registry for ``Octo/Demo`` lists the limit recorded for ``octo/demo``."""
        write_run("run-001", [_record("run-001", "cuda-absent", **_CUDA)])
        limits = TestKnownLimits._limits(tmp_path / "known-limits.json", "Octo/Demo")
        assert len(_rollup(tmp_path, limits)["known_limits"]) == 1


class TestSkipped:
    """Invalid ledgers are named, never counted, never silently dropped."""

    def test_invalid_ledger_is_named_and_not_counted(self, tmp_path: Path, write_run: Callable) -> None:
        """A ledger with a retired class value is listed under skipped and contributes nothing."""
        write_run("run-001", [_record("run-001", "lint-not-run")])
        bad = write_run("run-002", [_record("run-002", "opencv-absent", **{"class": "environment-inherent"})])
        data = _rollup(tmp_path)
        assert [item["path"] for item in data["skipped"]] == [str(bad)]
        assert (data["ledgers"], data["distinct_gaps"]) == (1, 1)

    def test_non_object_ledger_row_leaves_other_runs_available(self, tmp_path: Path, write_run: Callable) -> None:
        """Rollup keeps a valid run and names the malformed row in a separate ledger."""
        write_run("run-001", [_record("run-001", "lint-not-run")])
        bad = write_run("run-002", [])
        bad.write_text("[]\n", encoding="utf-8")

        data = _rollup(tmp_path, TestKnownLimits._limits(tmp_path / "known-limits.json"))

        assert (data["ledgers"], data["distinct_gaps"]) == (1, 1)
        (skipped,) = data["skipped"]
        assert skipped["path"] == str(bad)
        assert f"{bad}:1: record must be a JSON object" in skipped["error"]

    def test_route_and_version_identity_is_reported(self, tmp_path: Path, write_run: Callable) -> None:
        """Runs are broken down by harness, plugin version and route."""
        write_run("run-001", [_record("run-001", "lint-not-run")])
        assert _rollup(tmp_path)["routes"] == [
            {
                "harness": "claude",
                "plugin": "oss",
                "plugin_version": "0.69.0",
                "route": "direct",
                "runs": 1,
                "distinct_gaps": 1,
            },
        ]


_DAY = 24 * 60 * 60


def _age(run_dir: Path, seconds: float) -> None:
    """Set every file under a run directory to ``seconds`` old, so the in-progress window can be tested."""
    stamp = time.time() - seconds
    for path in run_dir.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))


def _codex_stub(run_dir: Path, checkpoint: str = "routed") -> None:
    """Write a Codex ``run-state.json`` stub into a new run directory."""
    run_dir.mkdir(parents=True)
    stub = {"schema_version": 1, "review_status": "incomplete", "checkpoint": checkpoint, "cause": "stopped"}
    (run_dir / "run-state.json").write_text(json.dumps(stub), encoding="utf-8")


def _write_review_artifacts(run_dir: Path, files: dict[str, Any]) -> None:
    """Write the selected review artifacts in their JSON form."""
    for name, content in files.items():
        (run_dir / name).write_text(json.dumps(content), encoding="utf-8")


def _review_run(reports: Path, pr: int, run: str, *files: str) -> Path:
    """Create a Claude review report directory ``.reports/review/pr-<pr>/<run>`` holding the named files."""
    run_dir = reports / ".reports" / "review" / f"pr-{pr}" / run
    run_dir.mkdir(parents=True)
    for name in files:
        (run_dir / name).write_text("x\n", encoding="utf-8")
    return run_dir


def _resolve_run(reports: Path, stamp: str, text: str | None) -> Path:
    """Create a Claude resolve run directory holding ``resolve-report.md`` with ``text`` (no file when ``None``)."""
    run_dir = reports / ".reports" / "resolve" / stamp
    run_dir.mkdir(parents=True)
    (run_dir / "notes.txt").write_text("x\n", encoding="utf-8")
    if text is not None:
        (run_dir / "resolve-report.md").write_text(text, encoding="utf-8")
    return run_dir


def _stub(pr: int, step: str) -> str:
    """Return a resolve report stub in the shape ``resolve_report_stub.py`` writes."""
    return (
        "<!-- resolve-report-status: incomplete -->\n\n"
        f"## Resolve Report — PR #{pr}\n\n"
        "**Status**: incomplete — this run has not written its final report yet.\n\n"
        f"**Last step reached**: {step}\n"
    )


def _final(pr: int) -> str:
    """Return the head of a final resolve report."""
    return f"## Resolve Report — PR #{pr}\n\n**Link**: https://github.com/octo/demo/pull/{pr}\n"


def _incomplete(tmp_path: Path) -> list[dict[str, Any]]:
    """Run the CLI over ``tmp_path`` and return its ``incomplete_runs`` rows."""
    out = tmp_path / "rollup-out" / "rollup.json"
    assert gap_rollup.main([str(tmp_path), "--format", "json", "--output", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))["incomplete_runs"]


class TestIncompleteRuns:
    """Runs that stopped before their terminal artifact are counted by harness, kind, checkpoint and status."""

    def test_relative_and_absolute_roots_count_each_run_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overlapping roots contribute one Claude run and one Codex run."""
        claude = _review_run(tmp_path, 7, "run-001", "head-sha.txt")
        codex = tmp_path / ".reports" / "codex" / "run-001"
        _codex_stub(codex)
        monkeypatch.chdir(tmp_path)

        runs = gap_rollup.find_incomplete_runs([Path(".reports"), tmp_path / ".reports"])
        assert sorted((run.harness, run.run_dir) for run in runs) == [("claude", claude), ("codex", codex)]

    @pytest.mark.parametrize("linked_root", [False, True])
    def test_checkout_root_includes_managed_review_allocations_and_ledgers(
        self, tmp_path: Path, linked_root: bool
    ) -> None:
        """A main or linked checkout finds allocations and ledgers in both report namespaces."""
        main = tmp_path / "main"
        common = main / ".git"
        (common / "objects").mkdir(parents=True)
        (common / "refs").mkdir()
        (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        checkout = tmp_path / "linked"
        checkout.mkdir()
        own = common / "worktrees" / "linked"
        own.mkdir(parents=True)
        (checkout / ".git").write_text(f"gitdir: {own.as_posix()}\n", encoding="utf-8")
        (own / "commondir").write_text("../..\n", encoding="utf-8")
        (own / "gitdir").write_text(f"{(checkout / '.git').as_posix()}\n", encoding="utf-8")
        (own / "HEAD").write_text("ref: refs/heads/linked\n", encoding="utf-8")
        managed = Path("codex-rig") / "reports" / "code-review"
        allocation = own / managed / "pr-7" / "run-001"
        allocation.mkdir(parents=True)
        entry = {"schema_version": 1, "allocated_as": allocation.name}
        _write_review_artifacts(allocation, {"review-entry.json": entry})
        completed = common / managed / "pr-8" / "run-001"
        completed.mkdir(parents=True)
        _write_review_artifacts(completed, {"review-entry.json": entry, "result.json": {}})
        ledger = completed / "gaps.jsonl"
        ledger.write_text(json.dumps(_record("run-001", "lint-not-run", pr=8)) + "\n", encoding="utf-8")

        roots = [checkout if linked_root else main]
        assert [run.run_dir for run in gap_rollup.find_incomplete_runs(roots)] == [allocation]
        assert gap_rollup.find_ledgers(roots) == [ledger]

    @pytest.mark.parametrize(
        ("identity", "files", "checkpoints"),
        [
            pytest.param({"pr_url": "https://github.com/octo/demo/pull/7"}, {}, ["allocated"], id="pr-allocation"),
            pytest.param({"local": {"scope": "working-tree"}}, {}, ["allocated"], id="local-allocation"),
            pytest.param({}, {}, ["allocated"], id="pr-allocation-without-url"),
            pytest.param(
                {},
                {"run-state.json": {"schema_version": 1, "review_status": "incomplete", "checkpoint": "routed"}},
                ["routed"],
                id="later-checkpoint-takes-precedence",
            ),
            pytest.param({}, {"result.json": {}}, [], id="terminal-result-excludes-allocation"),
        ],
    )
    def test_codex_review_allocation_counts_until_terminal_result(self, tmp_path, identity, files, checkpoints):
        """The review allocation counts once until a terminal result exists, even before routing."""
        run_dir = tmp_path / "code-review" / "2026-11-25T10-00-00-000001Z-pr-7-example"
        run_dir.mkdir(parents=True)
        entry = {"schema_version": 1, "allocated_as": run_dir.name, **identity}
        _write_review_artifacts(run_dir, {"review-entry.json": entry, **files})
        _age(run_dir, 3 * _DAY)

        runs = gap_rollup.find_incomplete_runs([tmp_path])
        assert [(run.harness, run.kind, run.checkpoint, run.status) for run in runs] == [
            ("codex", "review", checkpoint, "unfinished") for checkpoint in checkpoints
        ]

    def test_codex_stub_superseded_by_a_later_result_is_kept_apart(self, tmp_path: Path) -> None:
        """A stub with no ``result.json`` counts; a later run of the same PR with a result marks it superseded."""
        _codex_stub(tmp_path / "pr-7" / "run-001")
        _codex_stub(tmp_path / "pr-7" / "run-002")
        (tmp_path / "pr-7" / "run-002" / "result.json").write_text("{}", encoding="utf-8")
        assert _incomplete(tmp_path) == [
            {"harness": "codex", "kind": "review", "checkpoint": "routed", "status": "superseded", "runs": 1}
        ]

    def test_old_codex_stub_without_a_later_result_is_unfinished(self, tmp_path: Path) -> None:
        """A stopped run nothing replaced, older than the in-progress window, is lost work."""
        run_dir = tmp_path / "pr-7" / "run-001"
        _codex_stub(run_dir)
        _age(run_dir, 3 * _DAY)
        assert [row["status"] for row in _incomplete(tmp_path)] == ["unfinished"]

    def test_recent_run_is_in_progress(self, tmp_path: Path) -> None:
        """A run that wrote within 24 hours may still be running, so it is not counted as unfinished."""
        _codex_stub(tmp_path / "pr-7" / "run-001")
        assert [row["status"] for row in _incomplete(tmp_path)] == ["in-progress"]

    @pytest.mark.parametrize(
        ("files", "checkpoint"),
        [
            pytest.param(("head-sha.txt",), "allocated", id="allocated"),
            pytest.param(("head-sha.txt", "lineup.md"), "fan-out", id="fan-out"),
            pytest.param(("head-sha.txt", "lineup.md", "gap-prepare.txt"), "consolidation", id="consolidation"),
            pytest.param(("head-sha.txt", "review-report.refused.md"), "publication-refused", id="refused"),
        ],
    )
    def test_claude_review_without_report_counts_at_its_furthest_stage(
        self, tmp_path: Path, files: tuple[str, ...], checkpoint: str
    ) -> None:
        """A review report directory without ``review-report.md`` is an incomplete Claude review run."""
        run_dir = _review_run(tmp_path, 12, "run-001", *files)
        _age(run_dir, 3 * _DAY)
        assert _incomplete(tmp_path) == [
            {"harness": "claude", "kind": "review", "checkpoint": checkpoint, "status": "unfinished", "runs": 1}
        ]

    def test_claude_review_split_superseded_purged_unfinished(self, tmp_path: Path) -> None:
        """Three stopped review runs: one rerun to a finished later run, one emptied, one lost; finished runs skip."""
        for run in ("run-001", "run-002"):
            _age(_review_run(tmp_path, 12, run, "head-sha.txt"), 3 * _DAY)
        _review_run(tmp_path, 12, "run-003", "head-sha.txt", "review-report.md")
        _review_run(tmp_path, 13, "run-001")
        _age(_review_run(tmp_path, 14, "run-001", "head-sha.txt", "lineup.md"), 3 * _DAY)
        rows = {(row["checkpoint"], row["status"]): row["runs"] for row in _incomplete(tmp_path)}
        assert rows == {("allocated", "superseded"): 2, ("-", "purged"): 1, ("fan-out", "unfinished"): 1}

    def test_codex_review_directories_are_not_read_as_claude_runs(self, tmp_path: Path) -> None:
        """``.reports/codex/code-review/pr-N/run-NNN`` without ``review-report.md`` is not a Claude review run."""
        run_dir = tmp_path / ".reports" / "codex" / "code-review" / "pr-12" / "run-001"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text("{}", encoding="utf-8")
        assert gap_rollup.find_incomplete_runs([tmp_path]) == []

    def test_resolve_stub_counts_by_step_and_a_later_final_report_supersedes_it(self, tmp_path: Path) -> None:
        """A resolve stub counts at its ``Step N``; a later final report of the same PR marks it superseded."""
        stale = _resolve_run(
            tmp_path, "2026-11-20T10-00-00Z", _stub(12, "Step 10 — push step reached (push intent ask)")
        )
        lost = _resolve_run(tmp_path, "2026-11-20T11-00-00Z", _stub(13, "Step 9 — lint and QA gate started"))
        _resolve_run(tmp_path, "2026-11-21T10-00-00Z", _final(12))
        for run_dir in (stale, lost):
            _age(run_dir, 3 * _DAY)
        rows = {(row["kind"], row["checkpoint"], row["status"]) for row in _incomplete(tmp_path)}
        assert rows == {("resolve", "Step 10", "superseded"), ("resolve", "Step 9", "unfinished")}

    def test_resolve_run_root_checks_its_later_siblings(self, tmp_path: Path) -> None:
        """Supersession is the same when the root is the incomplete run or its report directory."""
        run_dir = _resolve_run(tmp_path, "2026-11-20T10-00-00Z", _stub(12, "Step 9"))
        _resolve_run(tmp_path, "2026-11-21T10-00-00Z", _final(12))
        _age(run_dir, 3 * _DAY)

        (direct,) = gap_rollup.find_incomplete_runs([run_dir])
        (recursive,) = gap_rollup.find_incomplete_runs([run_dir.parent])
        assert direct.status == recursive.status == "superseded"

    def test_resolve_run_without_any_report_counts(self, tmp_path: Path) -> None:
        """A resolve run directory whose report was never written is incomplete, at checkpoint ``no report``."""
        _age(_resolve_run(tmp_path, "2026-11-20T10-00-00Z", None), 3 * _DAY)
        assert [(row["checkpoint"], row["status"]) for row in _incomplete(tmp_path)] == [("no report", "unfinished")]

    def test_markdown_names_the_status_split(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The Markdown section totals each status and says only ``unfinished`` runs are lost work."""
        _age(_review_run(tmp_path, 12, "run-001", "head-sha.txt"), 3 * _DAY)
        assert gap_rollup.main([str(tmp_path)]) == 0
        out = capsys.readouterr().out.splitlines()
        assert "## Runs without a terminal artifact" in out
        [summary] = [line for line in out if line.startswith("unfinished: 1")]
        assert "superseded: 0" in summary
        assert "| claude | review | allocated | unfinished | 1 |" in out


class TestCli:
    """Command-line contract of the rollup."""

    def test_markdown_headline_lists_every_class(
        self, tmp_path: Path, write_run: Callable, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The headline table has one row per gap class and states the counting identity."""
        write_run("run-001", [_record("run-001", "lint-not-run")])
        assert gap_rollup.main([str(tmp_path)]) == 0
        rows = [line.split("|")[1].strip() for line in capsys.readouterr().out.splitlines() if line.startswith("| ")]
        assert rows[:7] == [
            "Class",
            "process",
            "environment-provisionable",
            "hardware-absent",
            "project",
            "contribution",
            "human-decision",
        ]

    def test_refuses_to_write_into_a_ledger(self, tmp_path: Path, write_run: Callable) -> None:
        """The rollup is a sidecar: an output path named like a ledger is an argument error."""
        write_run("run-001", [_record("run-001", "lint-not-run")])
        assert gap_rollup.main([str(tmp_path), "--output", str(tmp_path / "gaps.jsonl")]) == 2

    @pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")
    def test_refuses_a_report_symlink_to_a_ledger(self, tmp_path: Path, write_run: Callable) -> None:
        """A sidecar name cannot disguise an output that would overwrite a ledger."""
        ledger = write_run("run-001", [_record("run-001", "lint-not-run")])
        before = ledger.read_bytes()
        out = tmp_path / "rollup.json"
        out.symlink_to(ledger)

        assert gap_rollup.main([str(tmp_path), "--output", str(out)]) == 2
        assert ledger.read_bytes() == before

    def test_refuses_a_report_hard_link_to_a_ledger(self, tmp_path: Path, write_run: Callable) -> None:
        """An output that shares a ledger's inode is refused even without a symlink."""
        ledger = write_run("run-001", [_record("run-001", "lint-not-run")])
        before = ledger.read_bytes()
        out = tmp_path / "rollup.json"
        os.link(ledger, out)

        assert gap_rollup.main([str(tmp_path), "--output", str(out)]) == 2
        assert ledger.read_bytes() == before

    def test_exits_one_without_any_ledger(self, tmp_path: Path) -> None:
        """No ledger under the roots is reported as exit 1, not an empty success."""
        assert gap_rollup.main([str(tmp_path)]) == 1

    def test_writes_json_sidecar(self, tmp_path: Path, write_run: Callable) -> None:
        """``--format json --output`` writes the document to the sidecar path."""
        write_run("run-001", [_record("run-001", "lint-not-run")])
        out = tmp_path / "rollup" / "gaps-rollup.json"
        assert gap_rollup.main([str(tmp_path), "--format", "json", "--output", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["distinct_gaps"] == 1
