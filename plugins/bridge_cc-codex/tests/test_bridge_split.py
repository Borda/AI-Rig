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
