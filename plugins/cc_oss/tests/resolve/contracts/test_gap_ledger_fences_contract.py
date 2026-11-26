"""Run the shipped gap-ledger fences of oss:resolve and the oss:review terminal reject against scratch run state.

The fences are extracted from the skill Markdown and run with the real helpers, so a reworded block that stops writing
the identity, stops carrying an identical-head run's gaps or turns an exit code into a stop fails here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _oss_shell_plugin import render_shell
from _text_io import bash_block_after, bash_path, write_lf

_PLUGIN = Path(__file__).resolve().parents[3]
_FINAL_REPORT = _PLUGIN / "skills" / "resolve" / "modes" / "final-report.md"
_ACCEPTANCE = _PLUGIN / "skills" / "review" / "modes" / "acceptance-gate.md"
_AGENT_LAUNCH = _PLUGIN / "skills" / "review" / "modes" / "agent-launch.md"
_CONSOLIDATOR = _PLUGIN / "skills" / "review" / "templates" / "consolidator-prompt.md"
_SHARED = _PLUGIN / "skills" / "_shared"
_BASH = shutil.which("bash")
_GIT = shutil.which("git")
_HEAD = "c" * 40
_SESSION = "gap-fence"

pytestmark = pytest.mark.skipif(
    _BASH is None or _GIT is None or shutil.which("jq") is None, reason="fences are Bash with git and jq"
)


def _run_block(block: str, cwd: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run a shipped Bash block with the session id, plugin root and scratch TMPDIR a skill block sees."""
    env = os.environ | {
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": bash_path(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    return subprocess.run(
        [_BASH, "-c", render_shell(block, _PLUGIN, "oss")],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _sentinel(tmp_path: Path, name: str, value: str) -> None:
    """Write one session sentinel the fences read back with ``IFS= read -r``, LF only as the shipped producers write it.

    ``read -r`` keeps a carriage return in the value, so a platform newline would turn every path into a missing one.
    """
    write_lf(tmp_path / f"{name}-{_SESSION}", f"{value}\n")


def _repo(tmp_path: Path) -> Path:
    """Create an empty git repository the run works in."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([_GIT, "init", "-q"], cwd=repo, check=True)
    return repo


def _gap_line(summary: str = "full test suite failed to start") -> str:
    """Return one staged gap record holding gap fields only."""
    record = {
        "id": "full-suite-not-run",
        "class": "process",
        "owner": "workflow",
        "summary": summary,
        "closable_by": "rerun the full suite after the fixture repair",
        "evidence": "resolve-report.md",
        "deduction": 0.05,
    }
    return json.dumps(record) + "\n"


def _ledger(run_dir: Path) -> list[dict[str, object]]:
    """Read the ledger the fences appended."""
    lines = (run_dir / "gaps.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestResolveFences:
    """Step 11's Report file block prepares the ledger and its confirm block appends the staged gaps."""

    def _resolve_run(
        self, tmp_path: Path, repo: Path, stamp: str, head: str = _HEAD
    ) -> subprocess.CompletedProcess[str]:
        """Run the Report file block for one resolve run directory and return its output."""
        impl = tmp_path / f"impl-{stamp}"
        impl.mkdir()
        for name, value in {
            "resolve-impl-dir": str(impl),
            "resolve-pr-number": "12",
            "resolve-push-status": "pushed",
            "resolve-run-dir": f".reports/resolve/{stamp}",
            "resolve-pr-html-url": "https://github.com/octo/demo/pull/12",
            "resolve-pr-head-oid": head,
            "resolve-oss-shared": str(_SHARED),
        }.items():
            _sentinel(tmp_path, name, value)
        block = bash_block_after(_FINAL_REPORT.read_text(encoding="utf-8"), "**Report file — required.**")
        return _run_block(block, repo, tmp_path)

    def _confirm(self, tmp_path: Path, repo: Path) -> subprocess.CompletedProcess[str]:
        """Run the confirm-and-append block."""
        text = _FINAL_REPORT.read_text(encoding="utf-8")
        return _run_block(bash_block_after(text, "Then confirm the file on disk is the final report"), repo, tmp_path)

    def test_first_run_prepares_and_appends(self, tmp_path: Path) -> None:
        """The Report file block names the stage and prints the emission rules; the confirm block appends."""
        repo = _repo(tmp_path)
        prepared = self._resolve_run(tmp_path, repo, "2026-11-25T10-00-00Z")
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
        run_dir = repo / ".reports" / "resolve" / "2026-11-25T10-00-00Z"
        assert f"GAP_STAGE={Path('.reports/resolve/2026-11-25T10-00-00Z') / 'gaps.jsonl.rec'}" in prepared.stdout
        assert "GAP_ROUTE=none" in prepared.stdout
        assert "# Gap emission" in prepared.stdout
        (run_dir / "gaps.jsonl.rec").write_text(_gap_line(), encoding="utf-8")
        confirmed = self._confirm(tmp_path, repo)
        assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
        assert "GAP_LEDGER=appended 1" in confirmed.stdout
        [record] = _ledger(run_dir)
        assert record["run"]["run_id"] == "resolve-2026-11-25T10-00-00Z"
        assert record["run"]["kind"] == "resolve"
        assert record["subject"] == {"repo": "octo/demo", "pr": 12, "head": _HEAD}

    def test_identical_head_rerun_carries_and_new_head_records_fresh(self, tmp_path: Path) -> None:
        """A second resolve on the same head carries the open gap; a run on a new head starts fresh, exit 0."""
        repo = _repo(tmp_path)
        self._resolve_run(tmp_path, repo, "2026-11-25T10-00-00Z")
        first = repo / ".reports" / "resolve" / "2026-11-25T10-00-00Z"
        (first / "gaps.jsonl.rec").write_text(_gap_line(), encoding="utf-8")
        assert "GAP_LEDGER=appended 1" in self._confirm(tmp_path, repo).stdout
        again = self._resolve_run(tmp_path, repo, "2026-11-25T11-00-00Z")
        assert "GAP_CARRY=carried 1 from resolve-2026-11-25T10-00-00Z" in again.stdout
        assert "GAP_CARRIED=full-suite-not-run · process ·" in again.stdout
        assert "GAP_LEDGER=nothing staged" in self._confirm(tmp_path, repo).stdout
        carried = _ledger(repo / ".reports" / "resolve" / "2026-11-25T11-00-00Z")
        assert [record["carried_from"] for record in carried] == ["resolve-2026-11-25T10-00-00Z"]
        moved = self._resolve_run(tmp_path, repo, "2026-11-25T12-00-00Z", head="d" * 40)
        assert moved.returncode == 0
        assert "GAP_CARRY=fresh (head differs)" in moved.stdout

    def test_refused_stage_never_stops_the_confirm_block(self, tmp_path: Path) -> None:
        """A refused record makes the helper exit 1, but the confirm block itself still finishes."""
        repo = _repo(tmp_path)
        self._resolve_run(tmp_path, repo, "2026-11-25T10-00-00Z")
        run_dir = repo / ".reports" / "resolve" / "2026-11-25T10-00-00Z"
        (run_dir / "gaps.jsonl.rec").write_text('{"id": "Not A Slug"}\n', encoding="utf-8")
        confirmed = self._confirm(tmp_path, repo)
        assert "REPORT_STATE=" in confirmed.stdout
        assert "GAP_LEDGER=refused" in confirmed.stdout
        assert (run_dir / "gaps.jsonl.rec").exists()


class TestReviewRejectFences:
    """The terminal reject path prepares a route-``none`` ledger and appends the parent's staged gaps."""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create the repository, the allocated report directory and the review sentinels."""
        repo = _repo(tmp_path)
        report_dir = repo / ".reports" / "review" / "pr-12" / "run-001"
        report_dir.mkdir(parents=True)
        write_lf(report_dir / "head-sha.txt", f"{_HEAD}\n")
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "pr-meta.json").write_text(
            json.dumps({"url": "https://github.com/octo/demo/pull/12"}), encoding="utf-8"
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_lf(run_dir / "report-dir.txt", f"{report_dir}\n")
        write_lf(run_dir / "head-sha.txt", f"{_HEAD}\n")
        for name, value in {
            "oss-review-report-dir": str(report_dir),
            "oss-review-run-dir": str(run_dir),
            "oss-review-pr-tag": "12",
            "oss-review-snap-dir": str(snap),
            "review-oss-shared": str(_SHARED),
        }.items():
            _sentinel(tmp_path, name, value)
        return repo, report_dir

    def test_reject_read_back_prepares_and_append_block_appends(self, tmp_path: Path) -> None:
        """The read-back block prints the identity route and rules; the append block lands the staged gap."""
        repo, report_dir = self._setup(tmp_path)
        text = _ACCEPTANCE.read_text(encoding="utf-8")
        prepared = _run_block(
            bash_block_after(text, "Run the block below before writing either rejection."), repo, tmp_path
        )
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
        assert "GAP_ROUTE=none" in prepared.stdout
        assert "# Gap emission" in prepared.stdout
        (report_dir / "gaps.jsonl.rec").write_text(_gap_line("license provenance not checked"), encoding="utf-8")
        appended = _run_block(
            bash_block_after(text, "**Gap ledger** (`gap-emission.md`, printed above)"), repo, tmp_path
        )
        assert appended.returncode == 0, appended.stdout + appended.stderr
        assert "GAP_LEDGER=appended 1" in appended.stdout
        [record] = _ledger(report_dir)
        assert record["run"]["run_id"] == "pr-12-run-001"
        assert record["run"]["route"] == "none"


class TestReviewWiring:
    """The fan-out path prepares before consolidation and the consolidator stages and appends."""

    def test_boundary_block_prepares_with_the_watch_file(self) -> None:
        """The pre-consolidation block passes the report dir, head file and agent-watch file to prepare."""
        block = bash_block_after(_AGENT_LAUNCH.read_text(encoding="utf-8"), "**Gap ledger preparation**")
        [line] = [line for line in block.splitlines() if 'gap_emit.py" prepare' in line]
        for token in ("--kind review", '--dir "$_REPORT_DIR"', '--head-file "$_REPORT_DIR/head-sha.txt"'):
            assert token in line
        assert '--watch "$_RUN_DIR/agent-watch-review.tsv"' in line

    def test_consolidator_stages_and_appends_after_the_report(self) -> None:
        """The consolidator reads the prepare output, stages ``gaps.jsonl.rec`` and runs the append command."""
        lines = _CONSOLIDATOR.read_text(encoding="utf-8").splitlines()
        [line] = [line for line in lines if line.startswith("**Gap ledger — after the report is written.**")]
        for token in ("<REPORT_DIR>/gap-prepare.txt", "<REPORT_DIR>/gaps.jsonl.rec", 'gap_emit.py" append'):
            assert token in line
