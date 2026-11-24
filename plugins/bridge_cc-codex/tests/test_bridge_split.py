"""Cover the parallel split review: plan validation, one merged envelope, per-scope gaps and scratch cleanup.

The split runs ordinary ``bridge_call.py review`` children, so these tests copy the helper next to a stub
``bridge_call.py`` and drive the real script as a subprocess. Nothing here starts Codex.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"

_STUB = r"""
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
task = Path(args[args.index("--task-file") + 1]).read_text(encoding="utf-8")
log = Path(args[args.index("--workspace") + 1]) / "calls.jsonl"
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": args, "task": task}) + "\n")
if "HANG" in task:
    (Path(args[args.index("--workspace") + 1]) / "hang.pid").write_text(str(os.getpid()))
    time.sleep(600)
time.sleep(1.0)
if "SILENT" in task:
    sys.exit(3)
status = (
    "timeout" if "TIMEOUT" in task else "blocked" if "BLOCKED" in task else "partial" if "PARTIAL" in task else "complete"
)
findings = [f"finding for {task}"] if status in {"complete", "partial"} else []
blockers = ["hard cutoff reached"] if status == "timeout" else []
envelope = {"status": status, "findings": findings, "blockers": blockers, "run_id": args[args.index("--run-id") + 1]}
if status == "partial":
    envelope["remaining"] = ["unfinished part"]
print(json.dumps(envelope))
"""


@pytest.fixture
def split(tmp_path: Path) -> Path:
    """Copy the helper beside a stub child into a scratch bin directory and return the helper's path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("bridge_split.py", "bridge_task_path.py"):
        shutil.copy(_BIN / name, bin_dir / name)
    (bin_dir / "bridge_call.py").write_text(_STUB, encoding="utf-8")
    return bin_dir / "bridge_split.py"


def _run(split: Path, workspace: Path, tasks: object, *extra: str) -> tuple[int, dict[str, object], float]:
    """Run the helper on a plan file written into the workspace; return exit code, envelope and seconds taken."""
    plan = workspace / "plan.json"
    plan.write_text(json.dumps(tasks), encoding="utf-8")
    scratch = workspace / "tmp"
    scratch.mkdir(exist_ok=True)
    env = {**os.environ, "TMPDIR": str(scratch), "CLAUDE_CODE_SESSION_ID": "split-test"}
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(split), "--plan-file", str(plan), "--workspace", str(workspace), *extra],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return done.returncode, json.loads(done.stdout), time.monotonic() - started


class TestPlan:
    """Only a small JSON array of non-empty task strings is a valid plan."""

    @pytest.mark.parametrize(
        ("tasks", "message"),
        [
            pytest.param({"a": 1}, "JSON array", id="object"),
            pytest.param([], "JSON array", id="empty"),
            pytest.param(["ok", ""], "JSON array", id="blank-task"),
            pytest.param(["ok", 3], "JSON array", id="non-string"),
            pytest.param(["a", "b", "c", "d"], "at most 3", id="too-many"),
        ],
    )
    def test_invalid_plans_are_refused_before_any_child_runs(
        self, split: Path, tmp_path: Path, tasks: object, message: str
    ) -> None:
        """A malformed plan exits 2 with an error envelope and starts no child."""
        code, output, _ = _run(split, tmp_path, tasks)
        assert code == 2
        assert output["status"] == "error"
        assert message in str(output["error"])
        assert not (tmp_path / "calls.jsonl").exists()

    def test_a_plan_outside_the_workspace_and_temp_directory_is_refused(self, split: Path, tmp_path: Path) -> None:
        """A plan elsewhere on disk is not read, so a task cannot be pulled from an arbitrary path."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        plan = outside / "plan.json"
        plan.write_text('["a"]', encoding="utf-8")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (tmp_path / "other-tmp").mkdir()
        done = subprocess.run(
            [sys.executable, str(split), "--plan-file", str(plan), "--workspace", str(workspace)],
            capture_output=True,
            text=True,
            env={**os.environ, "TMPDIR": str(tmp_path / "other-tmp")},
            timeout=30,
            check=False,
        )
        assert done.returncode == 2
        assert "inside the workspace or directly in the temporary directory" in json.loads(done.stdout)["error"]

    @pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
    def test_a_linked_plan_is_refused(self, split: Path, tmp_path: Path) -> None:
        """A symbolic link as the plan file is refused rather than followed."""
        real = tmp_path / "real.json"
        real.write_text('["a"]', encoding="utf-8")
        link = tmp_path / "plan.json"
        link.symlink_to(real)
        env = {**os.environ, "TMPDIR": str(tmp_path)}
        done = subprocess.run(
            [sys.executable, str(split), "--plan-file", str(link), "--workspace", str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        assert done.returncode == 2
        assert "regular file" in json.loads(done.stdout)["error"]

    def test_the_soft_budget_cannot_exceed_a_synchronous_callers_limit(self, split: Path, tmp_path: Path) -> None:
        """A split is for a caller that must wait in turn, so a budget above 230 seconds is refused."""
        code, output, _ = _run(split, tmp_path, ["a"], "--timeout-seconds", "300")
        assert code == 2
        assert "at most 230" in str(output["error"])


class TestRun:
    """Scopes run side by side as ordinary review children and fold into one envelope."""

    def test_scopes_run_in_parallel_and_all_complete(self, split: Path, tmp_path: Path) -> None:
        """Three one-second children finish in about one second and merge to ``complete`` with tagged findings."""
        code, output, seconds = _run(split, tmp_path, ["alpha", "beta", "gamma"], "--run-id", "unit")
        assert code == 0
        assert output["status"] == "complete"
        assert seconds < 2.8
        assert [row["run_id"] for row in output["splits"]] == ["unit-s1", "unit-s2", "unit-s3"]
        assert [item["split"] for item in output["findings"]] == [1, 2, 3]
        assert output["blockers"] == []

    def test_each_child_gets_the_review_verb_and_its_own_task_and_bounds(self, split: Path, tmp_path: Path) -> None:
        """Every child is a review call with its own task file, the chosen effort and budget, and a distinct run id."""
        _run(split, tmp_path, ["alpha", "beta"], "--effort", "low", "--timeout-seconds", "100", "--untrusted-task")
        calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        assert sorted(call["task"] for call in calls) == ["alpha", "beta"]
        for call in calls:
            argv = call["argv"]
            assert argv[0] == "review"
            assert argv[argv.index("--effort") + 1] == "low"
            assert argv[argv.index("--timeout-seconds") + 1] == "100"
            assert "--untrusted-task" in argv
        assert len({call["argv"][call["argv"].index("--run-id") + 1] for call in calls}) == 2

    def test_a_timed_out_scope_is_a_visible_gap_not_a_pass(self, split: Path, tmp_path: Path) -> None:
        """One scope timing out makes the merge ``partial`` and names the gap in the merged blockers."""
        code, output, _ = _run(split, tmp_path, ["alpha", "TIMEOUT beta"])
        assert code == 0
        assert output["status"] == "partial"
        assert output["blockers"] == ["split 2: hard cutoff reached"]
        assert [item["split"] for item in output["findings"]] == [1]

    @pytest.mark.parametrize(
        ("tasks", "status"),
        [
            pytest.param(["TIMEOUT a", "TIMEOUT b"], "timeout", id="all-timeout"),
            pytest.param(["BLOCKED a", "BLOCKED b"], "blocked", id="all-blocked"),
            pytest.param(["TIMEOUT a", "BLOCKED b"], "blocked", id="mixed-gaps"),
        ],
    )
    def test_no_completed_scope_is_never_complete(
        self, split: Path, tmp_path: Path, tasks: list[str], status: str
    ) -> None:
        """When nothing completes the merge carries the shared failure status, or ``blocked`` when they differ."""
        _, output, _ = _run(split, tmp_path, tasks)
        assert output["status"] == status
        assert output["findings"] == []

    def test_a_child_without_an_envelope_is_an_error_row(self, split: Path, tmp_path: Path) -> None:
        """A child that prints nothing becomes an ``error`` row naming its exit code, and the others still count."""
        _, output, _ = _run(split, tmp_path, ["alpha", "SILENT beta"])
        assert output["status"] == "partial"
        rows = output["splits"]
        assert rows[1]["status"] == "error"
        assert "exit 3" in rows[1]["blockers"][0]

    def test_every_child_failing_is_blocked_not_a_refused_plan(self, split: Path, tmp_path: Path) -> None:
        """Scopes that all ran and failed merge to ``blocked``, so a caller never mistakes it for an unsupported split.

        A top-level ``error`` is reserved for a plan refused before any child ran (exit 2).
        """
        code, output, _ = _run(split, tmp_path, ["SILENT alpha", "SILENT beta"])
        assert code == 0
        assert output["status"] == "blocked"
        assert [row["status"] for row in output["splits"]] == ["error", "error"]

    def test_scratch_task_files_are_removed(self, split: Path, tmp_path: Path) -> None:
        """No generated task file stays in the temporary directory after the run, whatever each child did."""
        _run(split, tmp_path, ["alpha", "TIMEOUT beta", "SILENT gamma"])
        assert list((tmp_path / "tmp").glob("bridge-task-*")) == []


def _load(split: Path) -> object:
    """Import the helper copy beside the stub child, so its constants can be patched in-process."""
    spec = importlib.util.spec_from_file_location("bridge_split_under_test", split)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _process_exists(pid: int) -> bool:
    """Report whether a process with this id is still running, on every supported OS.

    ``os.kill(pid, 0)`` is a POSIX existence probe only: on Windows any signal other than the console-control ones calls
    ``TerminateProcess``, and a missing pid raises an ``OSError`` that Python 3.11 can wrap in ``SystemError``.
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True, check=True
    ).stdout
    return f'"{pid}"' in listing


class TestBackstop:
    """A scope that never exits is stopped by the split's own limit, and the other scopes still print."""

    def test_a_hung_scope_does_not_hide_the_finished_scopes(
        self, split: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One child that never exits becomes a timeout row at the backstop; the other scope's findings survive."""
        module = _load(split)
        monkeypatch.setattr(module, "ATTEMPT_OVERHEAD_SECONDS", 0.0)
        monkeypatch.setattr(module, "SCOPE_BACKSTOP_SLACK_SECONDS", 0.0)
        (tmp_path / "tmp").mkdir()
        monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "split-test")
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(["fine scope", "HANG scope"]), encoding="utf-8")
        code = module.main(["--plan-file", str(plan), "--workspace", str(tmp_path), "--timeout-seconds", "2"])
        output = json.loads(capsys.readouterr().out)
        rows = {row["index"]: row for row in output["splits"]}
        assert code == 0
        assert output["status"] == "partial"
        assert rows[1]["status"] == "complete"
        assert rows[2]["status"] == "timeout"
        assert any("backstop" in text for text in output["blockers"])
        assert not list((tmp_path / "tmp").glob("bridge-task-*"))
        pid = int((tmp_path / "hang.pid").read_text())
        assert not _process_exists(pid)  # the stopped child is gone, not left running

    def test_the_scope_limit_stays_below_the_synchronous_bash_timeout(self, split: Path) -> None:
        """The review skill gives a split 595 seconds, so the per-scope limit at the largest budget must be lower."""
        module = _load(split)
        worst = 2 * (module.HARD_CUTOFF_MULTIPLIER * module.DEFAULT_SOFT_SECONDS + module.ATTEMPT_OVERHEAD_SECONDS)
        assert worst + module.SCOPE_BACKSTOP_SLACK_SECONDS < 595


class TestIncident:
    """A quota stop in any scope is visible at the top level, where callers read one ``incident``."""

    @staticmethod
    def _record(workspace: Path, name: str, fault: str) -> str:
        """Write one incident record in the bridge incident store and return its workspace-relative path."""
        store = workspace / ".temp" / "bridge" / "incidents"
        store.mkdir(parents=True, exist_ok=True)
        (store / name).write_text(json.dumps({"fault": fault}), encoding="utf-8")
        return f".temp/bridge/incidents/{name}"

    def test_a_quota_record_wins_over_an_earlier_timeout_record(self, split: Path, tmp_path: Path) -> None:
        """The merged envelope names the quota-exhausted scope's record even when an earlier scope timed out."""
        module = _load(split)
        timeout = self._record(tmp_path, "1-timeout.json", "timeout")
        quota = self._record(tmp_path, "2-quota.json", "quota-exhausted")
        rows = [
            {"index": 1, "status": "timeout", "incident": timeout},
            {"index": 2, "status": "blocked", "incident": quota},
        ]
        assert module.merge(rows, tmp_path)["incident"] == quota

    def test_without_a_quota_record_the_first_incident_is_kept(self, split: Path, tmp_path: Path) -> None:
        """Another fault keeps its own record at the top level, and no incident leaves it null."""
        module = _load(split)
        timeout = self._record(tmp_path, "1-timeout.json", "timeout")
        assert module.merge([{"index": 1, "status": "timeout", "incident": timeout}], tmp_path)["incident"] == timeout
        assert module.merge([{"index": 1, "status": "complete"}], tmp_path)["incident"] is None

    def test_a_record_outside_the_incident_store_is_never_read(self, split: Path, tmp_path: Path) -> None:
        """A path leaving the store cannot select the top-level quota record."""
        module = _load(split)
        (tmp_path / "elsewhere.json").write_text(json.dumps({"fault": "quota-exhausted"}), encoding="utf-8")
        chosen = module.merge([{"index": 1, "status": "blocked", "incident": "elsewhere.json"}], tmp_path)["incident"]
        assert chosen == "elsewhere.json"  # kept only as the first incident, never promoted as a quota record
        quota = self._record(tmp_path, "2-quota.json", "quota-exhausted")
        rows = [{"index": 1, "status": "blocked", "incident": "elsewhere.json"}, {"index": 2, "incident": quota}]
        assert module.merge(rows, tmp_path)["incident"] == quota


class TestAnchoredPlanRead:
    """The plan is read from one opened descriptor, so a swapped-in file is refused rather than waited on."""

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs need POSIX")
    def test_a_fifo_swapped_in_after_the_path_checks_is_refused_without_waiting(
        self, split: Path, tmp_path: Path
    ) -> None:
        """Opening a FIFO never blocks, and its inode is not a regular file."""
        module = _load(split)
        fifo = tmp_path / "plan.fifo"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="regular file"):
            module._read_anchored(fifo)

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="ownership is POSIX only")
    def test_a_file_owned_by_another_user_is_refused(
        self, split: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plan in a shared directory must belong to the current user."""
        module = _load(split)
        plan = tmp_path / "plan.json"
        plan.write_text('["a"]', encoding="utf-8")
        monkeypatch.setattr(module.os, "geteuid", lambda: os.stat(plan).st_uid + 1)
        with pytest.raises(ValueError, match="owned by the current user"):
            module._read_anchored(plan)

    def test_a_file_that_grew_past_the_limit_is_refused(self, split: Path, tmp_path: Path) -> None:
        """The size is checked on the opened inode, not on an earlier lstat."""
        module = _load(split)
        plan = tmp_path / "plan.json"
        plan.write_text(" " * (module.MAX_PLAN_BYTES + 1), encoding="utf-8")
        with pytest.raises(ValueError, match="larger than"):
            module._read_anchored(plan)


class TestPartialScope:
    """A scope that answered part of its question is completed with a gap, and its remainder is never lost."""

    def test_a_partial_scope_beside_a_timeout_merges_as_partial(self, split: Path, tmp_path: Path) -> None:
        """Partial plus timeout is ``partial`` with the partial scope's findings and remaining text kept."""
        code, output, _ = _run(split, tmp_path, ["PARTIAL scope", "TIMEOUT scope"])
        assert code == 0
        assert output["status"] == "partial"
        assert [item["split"] for item in output["findings"]] == [1]
        assert output["remaining"] == [{"split": 1, "remaining": "unfinished part"}]

    def test_every_scope_partial_is_partial_not_blocked(self, split: Path, tmp_path: Path) -> None:
        """Two partial scopes never read as a shared failure."""
        _, output, _ = _run(split, tmp_path, ["PARTIAL one", "PARTIAL two"])
        assert output["status"] == "partial"
        assert len(output["remaining"]) == 2


def _git(workspace: Path, *args: str) -> str:
    """Run one Git command in the workspace with a fixed identity and no signing; return its stdout."""
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _range_workspace(workspace: Path, files: int, lines: int) -> None:
    """Create a repository whose second commit adds ``files`` text files of ``lines`` lines each, and one binary."""
    _git(workspace, "init", "-q")
    # The harness files beside the repository are not part of the change under review.
    (workspace / ".git" / "info" / "exclude").write_text("bin/\ntmp/\ncalls.jsonl\n*partition.json\n", encoding="utf-8")
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(workspace, "add", "seed.txt")
    _git(workspace, "commit", "-q", "-m", "seed")
    for number in range(files):
        (workspace / f"f{number}.txt").write_text("line\n" * lines, encoding="utf-8")
    (workspace / "image.bin").write_bytes(b"\x00\x01\x02" * 50)
    _git(workspace, "add", "image.bin", *(f"f{number}.txt" for number in range(files)))
    _git(workspace, "commit", "-q", "-m", "files")


def _partition(
    split: Path, workspace: Path, spec: object, *extra: str, name: str = "partition.json"
) -> tuple[int, dict[str, object]]:
    """Run the helper on a partition file written into the workspace; return exit code and envelope."""
    plan = workspace / name
    plan.write_text(json.dumps(spec), encoding="utf-8")
    scratch = workspace / "tmp"
    scratch.mkdir(exist_ok=True)
    env = {**os.environ, "TMPDIR": str(scratch), "CLAUDE_CODE_SESSION_ID": "split-test"}
    done = subprocess.run(
        [sys.executable, str(split), "--partition-file", str(plan), "--workspace", str(workspace), *extra],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return done.returncode, json.loads(done.stdout)


def _digest(split: Path, workspace: Path, spec: object, max_weight: int = 600, max_files: int = 40) -> str:
    """Return the digest the first wave of ``spec`` reports, without running a child."""
    module = _load(split)
    plan = workspace / "digest-partition.json"
    plan.write_text(json.dumps(spec), encoding="utf-8")
    return module.partition_digest(module.read_partition(plan, workspace.resolve(), max_weight, max_files))


def _calls(workspace: Path) -> list[dict[str, object]]:
    """Return the stub child's recorded calls."""
    log = workspace / "calls.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def _files_in(task: object) -> list[str]:
    """Return the file names a group task lists in its JSON array."""
    lines = str(task).splitlines()
    return json.loads(lines[lines.index("Files in this part (JSON array):") + 1])


class TestPartition:
    """A range or file list is packed into size-bounded groups that run three at a time, one wave per call."""

    def test_pack_groups_fills_the_heaviest_files_first_and_keeps_oversized_files_alone(self, split: Path) -> None:
        """Groups stay within the line, file and byte budgets; a file over the line budget is never merged."""
        module = _load(split)
        assert module.pack_groups({"a": 5, "b": 4, "c": 3}, 8, 10) == [["a", "c"], ["b"]]
        assert module.pack_groups({"big": 50, "small": 1}, 8, 10) == [["big"], ["small"]]
        assert module.pack_groups({"a": 1, "b": 1, "c": 1}, 100, 2) == [["a", "b"], ["c"]]
        assert module.pack_groups({"aa": 1, "bb": 1, "cc": 1}, 100, 10, 12) == [["aa", "bb"], ["cc"]]

    def test_a_range_is_pinned_to_commits_and_run_in_waves_of_three(self, split: Path, tmp_path: Path) -> None:
        """Seven five-line files at a five-line budget make seven groups: waves of three, three and one."""
        _range_workspace(tmp_path, files=7, lines=5)
        spec = {"task": "Audit the release.", "range": "HEAD~1..HEAD"}
        first_commit, second_commit = _git(tmp_path, "rev-parse", "HEAD~1", "HEAD").splitlines()
        code, first = _partition(split, tmp_path, spec, "--max-weight", "5")
        assert code == 0
        assert (first["wave"], first["waves"], first["groups"]) == (1, 3, 7)
        assert [row["index"] for row in first["splits"]] == [1, 2, 3]
        tasks = [str(call["task"]) for call in _calls(tmp_path)]
        assert len(tasks) == 3
        assert all(task.startswith("Audit the release.") for task in tasks)
        assert all(f"Range: {first_commit}..{second_commit}" in task for task in tasks)
        assert all(len(_files_in(task)) == 1 and "image.bin" not in task for task in tasks)
        (tmp_path / "calls.jsonl").unlink()
        digest = str(first["partition_digest"])
        code, last = _partition(split, tmp_path, spec, "--max-weight", "5", "--wave", "3", "--expect-digest", digest)
        assert code == 0
        assert [row["index"] for row in last["splits"]] == [7]
        assert last["partition_digest"] == digest
        assert last["status"] == "complete"
        assert len(_calls(tmp_path)) == 1

    def test_a_wave_before_the_last_is_partial_and_names_the_groups_not_yet_run(
        self, split: Path, tmp_path: Path
    ) -> None:
        """A caller that reads only ``status`` never takes the first wave of several for a finished review."""
        _range_workspace(tmp_path, files=7, lines=5)
        spec = {"task": "Audit.", "range": "HEAD~1..HEAD"}
        _, first = _partition(split, tmp_path, spec, "--max-weight", "5")
        assert first["status"] == "partial"
        assert first["remaining"] == [{"waves": [2, 3], "groups": [4, 7]}]
        digest = str(first["partition_digest"])
        _, middle = _partition(split, tmp_path, spec, "--max-weight", "5", "--wave", "2", "--expect-digest", digest)
        assert middle["remaining"] == [{"waves": [3, 3], "groups": [7, 7]}]
        _, last = _partition(split, tmp_path, spec, "--max-weight", "5", "--wave", "3", "--expect-digest", digest)
        assert last["status"] == "complete"
        assert last["remaining"] == []

    def test_a_failed_group_in_the_last_wave_is_still_a_gap(self, split: Path, tmp_path: Path) -> None:
        """Completeness of the last wave is its own: a timed-out group keeps the wave from reading complete."""
        _range_workspace(tmp_path, files=1, lines=1)
        (tmp_path / "f0.txt").write_text("TIMEOUT\n", encoding="utf-8")
        _git(tmp_path, "add", "f0.txt")
        _git(tmp_path, "commit", "-q", "-m", "again")
        code, output = _partition(split, tmp_path, {"task": "TIMEOUT audit.", "range": "HEAD~1..HEAD"})
        assert code == 0
        assert output["status"] == "timeout"
        assert output["splits"][0]["status"] == "timeout"

    def test_every_changed_text_file_lands_in_exactly_one_group(self, split: Path, tmp_path: Path) -> None:
        """Across all waves each text file is named once, and the binary file is never sent."""
        _range_workspace(tmp_path, files=5, lines=4)
        spec = {"task": "Audit.", "range": "HEAD~1..HEAD"}
        digest = _digest(split, tmp_path, spec, max_weight=4)
        for wave in (1, 2):
            code, _ = _partition(
                split, tmp_path, spec, "--max-weight", "4", "--wave", str(wave), "--expect-digest", digest
            )
            assert code == 0
        named = [name for call in _calls(tmp_path) for name in _files_in(call["task"])]
        assert sorted(named) == [f"f{number}.txt" for number in range(5)]

    def test_the_working_tree_diff_is_weighed_by_changed_lines_and_includes_a_deleted_file(
        self, split: Path, tmp_path: Path
    ) -> None:
        """``against`` lists what differs from a commit: an edited and a deleted file, never an untouched one."""
        _range_workspace(tmp_path, files=3, lines=500)
        (tmp_path / "f0.txt").write_text("line\n" * 500 + "added\n", encoding="utf-8")
        (tmp_path / "f1.txt").unlink()
        code, output = _partition(split, tmp_path, {"task": "Audit the diff.", "against": "HEAD"})
        assert code == 0
        assert output["groups"] == 1
        assert [item["path"] for item in output["skipped"]] == []
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert sorted(_files_in(task)) == ["f0.txt", "f1.txt"]
        assert f"Working tree against {_git(tmp_path, 'rev-parse', 'HEAD')}" in task

    def test_an_edit_between_waves_of_a_working_tree_partition_is_refused(self, split: Path, tmp_path: Path) -> None:
        """The digest covers the diff itself, so a fix applied after wave one voids the later waves."""
        _range_workspace(tmp_path, files=7, lines=5)
        (tmp_path / "f0.txt").write_text("changed\n" * 5, encoding="utf-8")
        for number in range(1, 7):
            (tmp_path / f"f{number}.txt").write_text("changed\n" * 5, encoding="utf-8")
        spec = {"task": "Audit.", "against": "HEAD"}
        _, first = _partition(split, tmp_path, spec, "--max-weight", "10")
        (tmp_path / "f0.txt").write_text("changed again\n" * 5, encoding="utf-8")
        code, refused = _partition(
            split,
            tmp_path,
            spec,
            "--max-weight",
            "10",
            "--wave",
            "2",
            "--expect-digest",
            str(first["partition_digest"]),
        )
        assert code == 2
        assert "changed" in str(refused["error"])

    def test_untracked_files_are_reviewed_whole_or_listed_as_skipped(self, split: Path, tmp_path: Path) -> None:
        """A new file is part of a working-tree change: a text one is grouped, a binary one is skipped, never
        dropped."""
        _range_workspace(tmp_path, files=1, lines=2)
        (tmp_path / "f0.txt").write_text("line\n" * 2 + "edited\n", encoding="utf-8")
        (tmp_path / "new_module.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        (tmp_path / "blob.dat").write_bytes(b"\x00\x01" * 20)
        code, output = _partition(split, tmp_path, {"task": "Audit the diff.", "against": "HEAD"})
        assert code == 0
        assert [item["path"] for item in output["skipped"]] == ["blob.dat"]
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert sorted(_files_in(task)) == ["f0.txt", "new_module.py"]

    def test_an_edit_to_an_untracked_file_between_waves_is_refused(self, split: Path, tmp_path: Path) -> None:
        """The digest covers new files too, so a fix applied to one after wave one voids the later waves."""
        _range_workspace(tmp_path, files=1, lines=2)
        for number in range(7):
            (tmp_path / f"new{number}.py").write_text("x = 1\n" * 5, encoding="utf-8")
        spec = {"task": "Audit.", "against": "HEAD"}
        first = _digest(split, tmp_path, spec, max_weight=5)
        (tmp_path / "new0.py").write_text("x = 2\n" * 5, encoding="utf-8")
        code, refused = _partition(split, tmp_path, spec, "--max-weight", "5", "--wave", "2", "--expect-digest", first)
        assert code == 2
        assert "changed" in str(refused["error"])

    def test_a_submodule_and_a_file_git_hides_are_skipped_never_grouped(self, split: Path, tmp_path: Path) -> None:
        """``git diff`` cannot show a submodule's contents or an assume-unchanged file, so no group claims them."""
        _range_workspace(tmp_path, files=2, lines=3)
        nested = tmp_path / "sm"
        nested.mkdir()
        _git(nested, "init", "-q")
        (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
        _git(nested, "add", "inner.txt")
        _git(nested, "commit", "-q", "-m", "inner")
        _git(tmp_path, "add", "sm")
        _git(tmp_path, "commit", "-q", "-m", "submodule")
        (nested / "inner.txt").write_text("inner\nedited\n", encoding="utf-8")
        _git(tmp_path, "update-index", "--assume-unchanged", "f1.txt")
        (tmp_path / "f1.txt").write_text("hidden edit\n", encoding="utf-8")
        (tmp_path / "f0.txt").write_text("visible edit\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"})
        assert code == 0
        reasons = {item["path"]: item["reason"] for item in output["skipped"]}
        assert "submodule" in reasons["sm"]
        assert "git hides" in reasons["f1.txt"]
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert _files_in(task) == ["f0.txt"]

    def test_the_reviews_own_files_never_count_as_changes(self, split: Path, tmp_path: Path) -> None:
        """Bridge transcripts under ``.temp`` and the partition file itself are not part of the working-tree change."""
        _range_workspace(tmp_path, files=1, lines=2)
        (tmp_path / "f0.txt").write_text("edited\n", encoding="utf-8")
        (tmp_path / ".temp" / "bridge").mkdir(parents=True)
        (tmp_path / ".temp" / "bridge" / "run.json").write_text("{}\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"}, name="review-spec.json")
        assert code == 0
        assert output["skipped"] == []
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert _files_in(task) == ["f0.txt"]

    def test_a_path_removed_from_the_index_but_kept_on_disk_is_listed_once(self, split: Path, tmp_path: Path) -> None:
        """A tracked deletion and an untracked file of one name are one entry: the file as it is on disk."""
        _range_workspace(tmp_path, files=1, lines=2)
        _git(tmp_path, "rm", "--cached", "-q", "image.bin")
        (tmp_path / "image.bin").write_text("now text\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"})
        assert code == 0
        assert [item["path"] for item in output["skipped"]] == []
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert "image.bin" in _files_in(task)
        assert "shows only as deleted while it still exists" in task

    def test_a_submodule_is_skipped_in_a_range_and_when_it_is_removed(self, split: Path, tmp_path: Path) -> None:
        """A submodule bump or removal shows only a pointer line, so no group claims to review its contents."""
        _range_workspace(tmp_path, files=1, lines=2)
        base = _git(tmp_path, "rev-parse", "HEAD")
        nested = tmp_path / "sm"
        nested.mkdir()
        _git(nested, "init", "-q")
        (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
        _git(nested, "add", "inner.txt")
        _git(nested, "commit", "-q", "-m", "inner")
        (tmp_path / "x.txt").write_text("one\n", encoding="utf-8")
        _git(tmp_path, "add", "sm", "x.txt")
        _git(tmp_path, "commit", "-q", "-m", "submodule")
        added = _git(tmp_path, "rev-parse", "HEAD")
        (tmp_path / "x.txt").write_text("two\n", encoding="utf-8")
        _git(tmp_path, "rm", "--cached", "-q", "sm")
        _git(tmp_path, "add", "x.txt")
        _git(tmp_path, "commit", "-q", "-m", "unlink")
        for revisions in (f"{base}..{added}", f"{added}..HEAD"):
            (tmp_path / "calls.jsonl").unlink(missing_ok=True)
            code, output = _partition(split, tmp_path, {"task": "Audit.", "range": revisions})
            assert code == 0, revisions
            reasons = {item["path"]: item["reason"] for item in output["skipped"]}
            assert "submodule" in reasons["sm"], revisions
            (task,) = (str(call["task"]) for call in _calls(tmp_path))
            assert _files_in(task) == ["x.txt"]

    def test_a_workspace_below_the_repository_root_lists_only_its_own_files(self, split: Path, tmp_path: Path) -> None:
        """Names are relative to the bound workspace, and a changed file outside it is no part of the review."""
        _range_workspace(tmp_path, files=0, lines=1)
        base = _git(tmp_path, "rev-parse", "HEAD")
        inside = tmp_path / "sub"
        inside.mkdir()
        (inside / "a.txt").write_text("a\n", encoding="utf-8")
        (tmp_path / "top.txt").write_text("t\n", encoding="utf-8")
        _git(tmp_path, "add", "sub/a.txt", "top.txt")
        _git(tmp_path, "commit", "-q", "-m", "change")
        (inside / "new.txt").write_text("n\n", encoding="utf-8")
        listed = {}
        for source, spec in (("range", {"range": f"{base}..HEAD"}), ("against", {"against": base})):
            (inside / "calls.jsonl").unlink(missing_ok=True)
            code, _ = _partition(split, inside, {"task": "Audit.", **spec})
            assert code == 0, source
            listed[source] = sorted(name for call in _calls(inside) for name in _files_in(call["task"]))
        assert listed == {"range": ["a.txt"], "against": ["a.txt", "new.txt"]}

    def test_a_partition_file_name_that_reads_as_a_glob_hides_no_real_change(self, split: Path, tmp_path: Path) -> None:
        """The partition file's own path is excluded literally, so ``p[ab].json`` never drops the real ``pa.json``."""
        _range_workspace(tmp_path, files=1, lines=2)
        (tmp_path / "pa.json").write_text("{}\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"}, name="p[ab].json")
        assert code == 0
        assert output["skipped"] == []
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert _files_in(task) == ["pa.json"]

    def test_a_hidden_flag_on_an_unchanged_file_lists_nothing(self, split: Path, tmp_path: Path) -> None:
        """An assume-unchanged file that holds what the commit holds has no change to hide and is not reported."""
        _range_workspace(tmp_path, files=2, lines=3)
        _git(tmp_path, "update-index", "--assume-unchanged", "f1.txt")
        (tmp_path / "f0.txt").write_text("edit\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"})
        assert code == 0
        assert output["skipped"] == []

    def test_a_file_named_twice_through_a_link_is_reviewed_once(self, split: Path, tmp_path: Path) -> None:
        """In a ``paths`` list a link and its target are one file, so only one name reaches a group."""
        (tmp_path / "t.txt").write_text("a\nb\n", encoding="utf-8")
        (tmp_path / "l.txt").symlink_to("t.txt")
        code, _ = _partition(split, tmp_path, {"task": "Audit.", "paths": ["l.txt", "t.txt"]})
        assert code == 0
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert _files_in(task) == ["l.txt"]

    def test_a_binary_file_in_a_paths_list_is_skipped(self, split: Path, tmp_path: Path) -> None:
        """A file with no line count is skipped whatever names it: here a NUL-bearing file in ``paths``."""
        (tmp_path / "keep.txt").write_text("a\nb\n", encoding="utf-8")
        (tmp_path / "blob.dat").write_bytes(b"\x00\n" * 30)
        code, output = _partition(split, tmp_path, {"task": "Audit.", "paths": ["keep.txt", "blob.dat"]})
        assert code == 0
        assert [item["path"] for item in output["skipped"]] == ["blob.dat"]
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert _files_in(task) == ["keep.txt"]

    def test_files_without_a_line_count_are_listed_as_skipped(self, split: Path, tmp_path: Path) -> None:
        """A binary file and a ``-diff`` lock file reach no group, and every envelope names them."""
        _range_workspace(tmp_path, files=1, lines=2)
        (tmp_path / ".gitattributes").write_text("*.lock -diff\n", encoding="utf-8")
        (tmp_path / "uv.lock").write_text("dep\n" * 5, encoding="utf-8")
        _git(tmp_path, "add", ".gitattributes", "uv.lock")
        _git(tmp_path, "commit", "-q", "-m", "lock")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "range": "HEAD~2..HEAD"})
        assert code == 0
        assert {item["path"] for item in output["skipped"]} == {"image.bin", "uv.lock"}
        named = [name for call in _calls(tmp_path) for name in _files_in(call["task"])]
        assert sorted(named) == [".gitattributes", "f0.txt"]

    @pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot hold a line feed")
    def test_a_file_name_that_is_not_printable_is_skipped_never_sent(self, split: Path, tmp_path: Path) -> None:
        """A name with a line feed cannot be placed in a task safely; it is skipped and reported."""
        _range_workspace(tmp_path, files=1, lines=2)
        (tmp_path / "bad\nname.txt").write_text("x\n", encoding="utf-8")
        _git(tmp_path, "add", "--", "bad\nname.txt")
        _git(tmp_path, "commit", "-q", "-m", "odd name")
        _, output = _partition(split, tmp_path, {"task": "Audit.", "range": "HEAD~2..HEAD"})
        reasons = {item["path"]: item["reason"] for item in output["skipped"]}
        assert reasons["bad\\nname.txt"] == "name is not a printable UTF-8 relative path"
        assert "bad" not in "".join(name for call in _calls(tmp_path) for name in _files_in(call["task"]))

    @pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot hold a line feed")
    def test_an_escaped_skipped_name_never_hides_a_real_file_of_that_spelling(
        self, split: Path, tmp_path: Path
    ) -> None:
        """A skipped name is shown escaped; a real file whose name is that escaped text is still a group member."""
        _range_workspace(tmp_path, files=1, lines=2)
        (tmp_path / "n\nl.txt").write_text("x\n", encoding="utf-8")
        (tmp_path / "n\\nl.txt").write_text("y\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"})
        assert code == 0
        assert [item["path"] for item in output["skipped"]] == ["n\\nl.txt"]
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert _files_in(task) == ["n\\nl.txt"]

    def test_a_submodule_stays_skipped_when_git_is_configured_to_ignore_submodules(
        self, split: Path, tmp_path: Path
    ) -> None:
        """``diff.ignoreSubmodules`` must not hide a changed gitlink from the listing: it is skipped, not dropped."""
        _range_workspace(tmp_path, files=1, lines=2)
        base = _git(tmp_path, "rev-parse", "HEAD")
        nested = tmp_path / "sm"
        nested.mkdir()
        _git(nested, "init", "-q")
        (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
        _git(nested, "add", "inner.txt")
        _git(nested, "commit", "-q", "-m", "inner")
        (tmp_path / "x.txt").write_text("one\n", encoding="utf-8")
        _git(tmp_path, "add", "sm", "x.txt")
        _git(tmp_path, "commit", "-q", "-m", "submodule")
        _git(tmp_path, "config", "diff.ignoreSubmodules", "all")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "range": f"{base}..HEAD"})
        assert code == 0
        assert [item["path"] for item in output["skipped"]] == ["sm"]

    def test_a_hard_link_and_its_target_are_one_file(self, split: Path, tmp_path: Path) -> None:
        """Two names for one file in a ``paths`` list review it once, whatever kind of link joins them."""
        (tmp_path / "t.txt").write_text("a\nb\n", encoding="utf-8")
        os.link(tmp_path / "t.txt", tmp_path / "hard.txt")
        code, _ = _partition(split, tmp_path, {"task": "Audit.", "paths": ["t.txt", "hard.txt"]})
        assert code == 0
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert _files_in(task) == ["t.txt"]

    @pytest.mark.skipif(os.name == "nt", reason="Windows has no executable bit to change")
    def test_a_mode_only_change_on_a_hidden_flag_file_is_not_dropped(self, split: Path, tmp_path: Path) -> None:
        """An assume-unchanged file whose only change is its executable bit is listed as skipped, not forgotten."""
        _range_workspace(tmp_path, files=2, lines=3)
        _git(tmp_path, "update-index", "--assume-unchanged", "f1.txt")
        (tmp_path / "f1.txt").chmod(0o755)
        (tmp_path / "f0.txt").write_text("edit\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"})
        assert code == 0
        assert [item["path"] for item in output["skipped"]] == ["f1.txt"]

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    def test_a_flagged_file_deleted_from_disk_is_skipped_not_forgotten(
        self, split: Path, tmp_path: Path, flag: str
    ) -> None:
        """A hidden-flag file removed from the working tree is a change git cannot show, so it is listed."""
        _range_workspace(tmp_path, files=2, lines=3)
        _git(tmp_path, "update-index", flag, "f1.txt")
        (tmp_path / "f1.txt").unlink()
        (tmp_path / "f0.txt").write_text("edit\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"})
        assert code == 0
        assert [item["path"] for item in output["skipped"]] == ["f1.txt"]
        (task,) = (str(call["task"]) for call in _calls(tmp_path))
        assert _files_in(task) == ["f0.txt"]

    @pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot hold a line feed")
    def test_a_flagged_file_with_an_unusable_name_is_skipped_in_escaped_form(self, split: Path, tmp_path: Path) -> None:
        """A hidden-flag file whose name cannot be shown safely is still reported, never dropped."""
        _range_workspace(tmp_path, files=1, lines=2)
        (tmp_path / "bad\nname.txt").write_text("x\n", encoding="utf-8")
        _git(tmp_path, "add", "--", "bad\nname.txt")
        _git(tmp_path, "commit", "-q", "-m", "odd name")
        _git(tmp_path, "update-index", "--assume-unchanged", "--", "bad\nname.txt")
        (tmp_path / "bad\nname.txt").write_text("changed\n", encoding="utf-8")
        (tmp_path / "f0.txt").write_text("edit\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"})
        assert code == 0
        assert [item["path"] for item in output["skipped"]] == ["bad\\nname.txt"]

    def test_the_reviews_own_files_are_left_out_of_the_hidden_file_check_too(self, split: Path, tmp_path: Path) -> None:
        """A tracked ``.temp`` file marked hidden and edited is the review's own file, never a skipped change."""
        _range_workspace(tmp_path, files=1, lines=2)
        (tmp_path / ".temp").mkdir()
        (tmp_path / ".temp" / "x.txt").write_text("one\n", encoding="utf-8")
        _git(tmp_path, "add", "-f", ".temp/x.txt")
        _git(tmp_path, "commit", "-q", "-m", "tracked run file")
        _git(tmp_path, "update-index", "--assume-unchanged", ".temp/x.txt")
        (tmp_path / ".temp" / "x.txt").write_text("two\n", encoding="utf-8")
        (tmp_path / "f0.txt").write_text("edit\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "Audit.", "against": "HEAD"})
        assert code == 0
        assert output["skipped"] == []

    def test_a_range_that_moves_between_waves_is_refused_by_the_expected_digest(
        self, split: Path, tmp_path: Path
    ) -> None:
        """``HEAD`` resolves to a commit, so a later commit changes the digest and a later wave starts no child."""
        _range_workspace(tmp_path, files=4, lines=5)
        spec = {"task": "Audit.", "range": "HEAD~1..HEAD"}
        _, first = _partition(split, tmp_path, spec, "--max-weight", "5")
        (tmp_path / "f0.txt").write_text("LINE\n" * 5, encoding="utf-8")
        _git(tmp_path, "add", "f0.txt")
        _git(tmp_path, "commit", "-q", "-m", "same size edit")
        (tmp_path / "calls.jsonl").unlink()
        code, output = _partition(
            split, tmp_path, spec, "--max-weight", "5", "--wave", "2", "--expect-digest", str(first["partition_digest"])
        )
        assert code == 2
        assert "partition changed since the first wave" in str(output["error"])
        assert not (tmp_path / "calls.jsonl").exists()

    def test_named_paths_are_partitioned_by_line_count_and_bound_to_their_bytes(
        self, split: Path, tmp_path: Path
    ) -> None:
        """A file list needs no Git history; a same-length edit changes the digest and ``./a.py`` is ``a.py``."""
        for name in ("a.py", "b.py", "c.py"):
            (tmp_path / name).write_text("x\n" * 6, encoding="utf-8")
        spec = {"task": "Review.", "paths": ["a.py", "./a.py", "b.py", "c.py"]}
        code, output = _partition(split, tmp_path, spec, "--max-weight", "6")
        assert code == 0
        assert (output["waves"], output["groups"], output["status"]) == (1, 3, "complete")
        assert sorted(name for call in _calls(tmp_path) for name in _files_in(call["task"])) == ["a.py", "b.py", "c.py"]
        assert all("changes in the files" not in str(call["task"]) for call in _calls(tmp_path))
        (tmp_path / "a.py").write_text("y\n" * 6, encoding="utf-8")
        _, edited = _partition(split, tmp_path, spec, "--max-weight", "6")
        assert edited["partition_digest"] != output["partition_digest"]

    def test_a_range_of_any_size_runs_in_as_many_waves_as_it_needs(self, split: Path, tmp_path: Path) -> None:
        """There is no group cap: 31 files at a two-line budget are 11 waves, and the last holds one group."""
        _range_workspace(tmp_path, files=31, lines=2)
        spec = {"task": "Audit.", "range": "HEAD~1..HEAD"}
        digest = _digest(split, tmp_path, spec, max_weight=2)
        code, last = _partition(split, tmp_path, spec, "--max-weight", "2", "--wave", "11", "--expect-digest", digest)
        assert code == 0
        assert (last["waves"], last["groups"]) == (11, 31)
        assert [row["index"] for row in last["splits"]] == [31]

    def test_no_child_task_exceeds_the_child_size_limit(self, split: Path, tmp_path: Path) -> None:
        """Long names are packed by their bytes, so every group's task fits the 16 KiB a review child accepts."""
        module = _load(split)
        source = (_BIN / "bridge_call.py").read_text(encoding="utf-8")
        assert f"MAX_TASK_UTF8_BYTES = {module.TASK_BYTE_LIMIT // 1024} * 1024" in source
        names = [f"{'n' * 150}{number:02d}.py" for number in range(60)]
        for name in names:
            (tmp_path / name).write_text("x\n", encoding="utf-8")
        spec = {"task": "T" * 14000, "paths": names}
        code, first = _partition(split, tmp_path, spec, "--max-weight", "1000", "--max-files", "1000")
        assert code == 0
        assert first["waves"] > 1
        (tmp_path / "calls.jsonl").unlink()
        for wave in range(1, int(first["waves"]) + 1):
            _partition(
                split,
                tmp_path,
                spec,
                *("--max-weight", "1000", "--max-files", "1000", "--wave", str(wave)),
                *("--expect-digest", str(first["partition_digest"])),
            )
        sizes = [len(str(call["task"]).encode("utf-8")) for call in _calls(tmp_path)]
        assert max(sizes) <= module.TASK_BYTE_LIMIT
        assert sorted(name for call in _calls(tmp_path) for name in _files_in(call["task"])) == sorted(names)

    def test_a_shared_task_that_leaves_no_room_for_file_names_is_refused(self, split: Path, tmp_path: Path) -> None:
        """A 16 KiB question cannot be repeated per part, so the partition is refused before any child runs."""
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        code, output = _partition(split, tmp_path, {"task": "T" * 17000, "paths": ["a.py"]})
        assert code == 2
        assert "too long to share" in str(output["error"])

    def test_a_non_final_wave_report_is_the_only_one_that_names_later_groups(self, split: Path) -> None:
        """The helper marks only an envelope that still has waves ahead of it."""
        module = _load(split)
        assert module.account_for_later_waves({"status": "complete", "wave": 1, "waves": 1})["status"] == "complete"
        later = module.account_for_later_waves(
            {"status": "blocked", "wave": 1, "waves": 2, "later_groups": [4, 5], "remaining": [{"split": 1}]}
        )
        assert later["status"] == "blocked"
        assert later["remaining"] == [{"split": 1}, {"waves": [2, 2], "groups": [4, 5]}]

    @pytest.mark.parametrize(
        ("spec", "message"),
        [
            pytest.param({"range": "HEAD~1..HEAD"}, '"task"', id="no-task"),
            pytest.param({"task": "t"}, "exactly one of", id="no-source"),
            pytest.param({"task": "t", "range": "HEAD", "paths": ["seed.txt"]}, "exactly one of", id="both-sources"),
            pytest.param(
                {"task": "t", "range": "HEAD~1..HEAD", "against": "HEAD"}, "exactly one of", id="range-and-against"
            ),
            pytest.param({"task": "t", "against": "HEAD..HEAD"}, "one plain Git revision", id="against-a-range"),
            pytest.param({"task": "t", "against": 7}, "must be a string", id="against-not-a-string"),
            pytest.param({"task": "t", "range": "HEAD"}, "A..B or A...B", id="single-revision"),
            pytest.param({"task": "t", "range": "--output=x"}, "A..B or A...B", id="option-shaped-range"),
            pytest.param({"task": "t", "range": "HEAD..--git-dir"}, "A..B or A...B", id="option-shaped-end"),
            pytest.param({"task": "t", "range": "..HEAD"}, "A..B or A...B", id="open-left-end"),
            pytest.param({"task": "t", "range": "a;b..HEAD"}, "A..B or A...B", id="shell-range"),
            pytest.param({"task": "t", "range": "nosuchref..HEAD"}, "git could not list", id="unknown-range"),
            pytest.param({"task": "t", "paths": ["../escape.txt"]}, "relative workspace path", id="path-escape"),
            pytest.param({"task": "t", "paths": ["/etc/hosts"]}, "relative workspace path", id="absolute-path"),
            pytest.param({"task": "t", "paths": ["missing.txt"]}, "not a file", id="missing-path"),
            pytest.param({"task": "t", "paths": ["a\nb"]}, "relative workspace path", id="control-character"),
            pytest.param({"task": "t", "paths": []}, "non-empty array", id="empty-paths"),
        ],
    )
    def test_a_malformed_partition_is_refused_before_any_child_runs(
        self, split: Path, tmp_path: Path, spec: object, message: str
    ) -> None:
        """Bad input exits 2 with an error envelope and starts no review."""
        _range_workspace(tmp_path, files=1, lines=1)
        code, output = _partition(split, tmp_path, spec)
        assert code == 2
        assert output["status"] == "error"
        assert message in str(output["error"])
        assert not (tmp_path / "calls.jsonl").exists()

    def test_a_wave_past_the_last_is_refused(self, split: Path, tmp_path: Path) -> None:
        """Asking for a wave the partition does not have is an error, never an empty success."""
        _range_workspace(tmp_path, files=2, lines=2)
        spec = {"task": "t", "range": "HEAD~1..HEAD"}
        code, output = _partition(
            split, tmp_path, spec, "--wave", "2", "--expect-digest", _digest(split, tmp_path, spec)
        )
        assert code == 2
        assert "between 1 and 1" in str(output["error"])

    def test_a_later_wave_without_the_first_waves_digest_is_refused(self, split: Path, tmp_path: Path) -> None:
        """A wave after the first runs only against the digest that proves it shares the first wave's inputs."""
        _range_workspace(tmp_path, files=7, lines=5)
        code, output = _partition(
            split, tmp_path, {"task": "t", "range": "HEAD~1..HEAD"}, "--max-weight", "5", "--wave", "2"
        )
        assert code == 2
        assert "needs --expect-digest" in str(output["error"])
        assert not (tmp_path / "calls.jsonl").exists()

    def test_wave_options_apply_only_to_a_partition(self, split: Path, tmp_path: Path) -> None:
        """A caller-written plan has no waves, so ``--wave`` with it is refused."""
        code, output, _ = _run(split, tmp_path, ["a"], "--wave", "2")
        assert code == 2
        assert "apply only to --partition-file" in str(output["error"])

    def test_the_two_plan_sources_exclude_each_other(self, split: Path, tmp_path: Path) -> None:
        """One call takes a caller-written plan or a partition file, never both."""
        (tmp_path / "plan.json").write_text('["a"]', encoding="utf-8")
        (tmp_path / "partition.json").write_text('{"task": "t", "paths": ["plan.json"]}', encoding="utf-8")
        done = subprocess.run(
            [
                sys.executable,
                str(split),
                "--plan-file",
                str(tmp_path / "plan.json"),
                "--partition-file",
                str(tmp_path / "partition.json"),
                "--workspace",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert done.returncode == 2
        assert "not allowed with" in done.stderr
