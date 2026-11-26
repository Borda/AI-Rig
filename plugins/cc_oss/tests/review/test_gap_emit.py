"""Contract tests for ``gap_emit.py``: run identity, carry-forward, known limits and staged-record append."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import gap_ledger
import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_HELPER = _PLUGIN / "bin" / "gap_emit.py"
_HEAD = "a" * 40
_OTHER_HEAD = "b" * 40
_PR_URL = "https://github.com/octo/demo/pull/12"
_GIT = shutil.which("git")
_ARCHITECTURE = {"amd64": "x86_64", "aarch64": "arm64"}.get(
    platform.machine().casefold(), platform.machine().casefold()
)


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the shipped helper with the given arguments from ``cwd``."""
    return subprocess.run(
        [sys.executable, str(_HELPER), *args], cwd=cwd, capture_output=True, text=True, check=False, encoding="utf-8"
    )


def _plugin_root(tmp_path: Path, version: str = "0.69.0") -> Path:
    """Create a fake installed plugin root carrying only its manifest version."""
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return root


def _review_dir(tmp_path: Path, run: str, head: str = _HEAD) -> Path:
    """Create one review report directory ``pr-12/<run>`` holding its ``head-sha.txt``."""
    run_dir = tmp_path / "repo" / ".reports" / "review" / "pr-12" / run
    run_dir.mkdir(parents=True)
    (run_dir / "head-sha.txt").write_text(f"{head}\n", encoding="utf-8")
    return run_dir


def _prepare_review(run_dir: Path, root: Path, watch: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``prepare`` for a review run directory the way the review's fixed block does."""
    args = ["prepare", "--kind", "review", "--dir", str(run_dir), "--plugin-root", str(root), "--pr", "12"]
    args += ["--pr-url", _PR_URL, "--head-file", str(run_dir / "head-sha.txt")]
    if watch is not None:
        args += ["--watch", str(watch)]
    return _run(*args, cwd=run_dir)


def _watch(tmp_path: Path, rows: str) -> Path:
    """Write an agent-watch file with the given rows."""
    path = tmp_path / "agent-watch-review.tsv"
    path.write_text(rows, encoding="utf-8")
    return path


def _stage(run_dir: Path, *records: dict[str, object]) -> None:
    """Stage gap-only records the way a writer does with the Write tool."""
    (run_dir / "gaps.jsonl.rec").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _gap(gap_id: str = "codex-co-review-quota-exhausted", **extra: object) -> dict[str, object]:
    """Return one gap record holding gap fields only, as the writer stages it."""
    record: dict[str, object] = {
        "id": gap_id,
        "class": "process",
        "owner": "workflow",
        "summary": "Codex co-review not run: quota-exhausted",
        "closable_by": "rerun the review after the Codex quota resets",
        "evidence": "foundry--codex.md",
        "deduction": 0.05,
    }
    return record | extra


def _ledger(run_dir: Path) -> list[dict[str, object]]:
    """Read the run's ledger records."""
    text = (run_dir / "gaps.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _supports_file_symlinks() -> bool:
    """Probe file symlinks using the same capability required by identity handling."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        target = path / "target"
        target.touch()
        try:
            (path / "link").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
    return True


class TestIdentity:
    """The helper derives the run identity from disk, so no writer ever types it."""

    def test_review_identity_names_repo_pr_head_version_and_direct_route(self, tmp_path: Path) -> None:
        """A review that spawned agents is ``direct`` and its run id is unique within the repository."""
        run_dir = _review_dir(tmp_path, "run-002")
        done = _prepare_review(run_dir, _plugin_root(tmp_path), _watch(tmp_path, "qa\t/x/qa.md\t1800\n"))
        assert done.returncode == 0, done.stdout + done.stderr
        identity = json.loads((run_dir / "gap-identity.json").read_text(encoding="utf-8"))
        assert identity == {
            "subject": {"repo": "octo/demo", "pr": 12, "head": _HEAD},
            "run": {
                "run_id": "pr-12-run-002",
                "kind": "review",
                "harness": "claude",
                "plugin": "oss",
                "plugin_version": "0.69.0",
                "route": "direct",
            },
        }
        assert "GAP_ROUTE=direct" in done.stdout
        assert (run_dir / "gap-prepare.txt").read_text(encoding="utf-8").splitlines() == done.stdout.splitlines()

    def test_repository_is_stored_case_folded(self, tmp_path: Path) -> None:
        """A PR URL spelling ``Octo/Demo`` records ``octo/demo``, the identity every other emitter uses."""
        run_dir = _review_dir(tmp_path, "run-001")
        args = ["prepare", "--kind", "review", "--dir", str(run_dir), "--plugin-root", str(_plugin_root(tmp_path))]
        args += ["--pr", "12", "--pr-url", "https://github.com/Octo/Demo/pull/12"]
        done = _run(*args, "--head-file", str(run_dir / "head-sha.txt"), cwd=run_dir)
        assert done.returncode == 0, done.stdout + done.stderr
        identity = json.loads((run_dir / "gap-identity.json").read_text(encoding="utf-8"))
        assert identity["subject"]["repo"] == "octo/demo"

    def test_review_without_spawned_agents_is_route_none(self, tmp_path: Path) -> None:
        """A terminal reject passes no watch file, so no reviewer was dispatched."""
        run_dir = _review_dir(tmp_path, "run-001")
        done = _prepare_review(run_dir, _plugin_root(tmp_path))
        assert "GAP_ROUTE=none" in done.stdout

    def test_later_prepare_keeps_the_first_identity(self, tmp_path: Path) -> None:
        """A late terminal reject after the fan-out keeps the route Step 2 recorded."""
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path), _watch(tmp_path, "qa\t/x/qa.md\t1800\n"))
        again = _prepare_review(run_dir, _plugin_root(tmp_path))
        assert "GAP_ROUTE=direct" in again.stdout

    @pytest.mark.parametrize(
        ("head", "message"),
        [
            pytest.param("", "head commit unavailable", id="missing-head"),
            pytest.param("not-a-sha", "head commit unavailable", id="malformed-head"),
        ],
    )
    def test_missing_identity_prints_unavailable_and_never_stops(self, tmp_path: Path, head: str, message: str) -> None:
        """An identity the helper cannot establish is a printed warning, exit 0, and no identity file."""
        run_dir = _review_dir(tmp_path, "run-001", head=head)
        done = _prepare_review(run_dir, _plugin_root(tmp_path))
        assert done.returncode == 0
        assert "⚠ GAP_LEDGER=unavailable" in done.stdout
        assert message in done.stdout
        assert not (run_dir / "gap-identity.json").exists()

    def test_empty_run_dir_argument_writes_nothing(self, tmp_path: Path) -> None:
        """An emptied sentinel never makes the helper write into the current directory."""
        done = _run("append", "--dir", "", cwd=tmp_path)
        assert done.returncode == 0
        assert "unavailable" in done.stdout
        assert list(tmp_path.iterdir()) == []


class TestAppend:
    """Staged gap-only records gain the run identity and land through the schema owner."""

    def test_append_fills_identity_and_deletes_the_stage(self, tmp_path: Path) -> None:
        """The appended record carries schema, subject and run although the writer staged gap fields only."""
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path), _watch(tmp_path, "qa\t/x\t1800\n"))
        _stage(run_dir, _gap())
        done = _run("append", "--dir", str(run_dir), cwd=run_dir)
        assert done.returncode == 0, done.stdout
        assert "GAP_LEDGER=appended 1" in done.stdout
        [record] = _ledger(run_dir)
        assert record["schema"] == "gap-ledger"
        assert record["schema_version"] == 1
        assert record["run"]["run_id"] == "pr-12-run-001"
        assert record["subject"]["head"] == _HEAD
        assert not (run_dir / "gaps.jsonl.rec").exists()

    def test_nothing_staged_is_not_an_error(self, tmp_path: Path) -> None:
        """A run with no gaps stages nothing and the append block still exits 0."""
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path))
        done = _run("append", "--dir", str(run_dir), cwd=run_dir)
        assert done.returncode == 0
        assert "GAP_LEDGER=nothing staged" in done.stdout

    def test_refused_record_exits_1_and_keeps_the_stage(self, tmp_path: Path) -> None:
        """A record the schema refuses appends nothing and stays staged for one correction."""
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path))
        _stage(run_dir, _gap(**{"class": "environment-inherent"}))
        done = _run("append", "--dir", str(run_dir), cwd=run_dir)
        assert done.returncode == 1
        assert "GAP_LEDGER=refused" in done.stdout
        assert (run_dir / "gaps.jsonl.rec").exists()
        assert not (run_dir / "gaps.jsonl").exists()

    def test_append_without_prepare_keeps_records_and_exits_0(self, tmp_path: Path) -> None:
        """Without an identity file nothing is appended, the stage stays, and the run continues."""
        run_dir = _review_dir(tmp_path, "run-001")
        _stage(run_dir, _gap())
        done = _run("append", "--dir", str(run_dir), cwd=run_dir)
        assert done.returncode == 0
        assert "⚠ GAP_LEDGER=unavailable" in done.stdout
        assert (run_dir / "gaps.jsonl.rec").exists()

    @pytest.mark.parametrize(
        "identity",
        [
            pytest.param("[" * 10_000 + "]" * 10_000, id="deeply-nested-json"),
            pytest.param('{"subject":null,"run":{}}', id="invalid-subject"),
            pytest.param('{"subject":{"repo":"octo/demo","pr":12,"head":"aaaaaaa"},"run":{}}', id="missing-run-keys"),
        ],
    )
    def test_malformed_identity_warns_and_preserves_stage(self, tmp_path: Path, identity: str) -> None:
        """Append validates the full frozen identity and leaves the evidence available on refusal."""
        run_dir = _review_dir(tmp_path, "run-001")
        (run_dir / "gap-identity.json").write_text(identity, encoding="utf-8")
        _stage(run_dir, _gap())
        stage = run_dir / "gaps.jsonl.rec"
        original = stage.read_bytes()

        done = _run("append", "--dir", str(run_dir), cwd=run_dir)

        assert done.returncode == 0
        assert "⚠ GAP_LEDGER=unavailable" in done.stdout
        assert "identity" in done.stdout
        assert not done.stderr
        assert stage.read_bytes() == original
        assert not (run_dir / "gaps.jsonl").exists()

    @pytest.mark.skipif(not _supports_file_symlinks(), reason="host cannot create file symlinks")
    def test_symlinked_identity_warns_and_preserves_stage(self, tmp_path: Path) -> None:
        """An otherwise valid identity from a linked file cannot authorize an append."""
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path))
        identity = run_dir / "gap-identity.json"
        target = identity.rename(tmp_path / "external-identity.json")
        identity.symlink_to(target)
        _stage(run_dir, _gap())
        stage = run_dir / "gaps.jsonl.rec"
        original = stage.read_bytes()

        done = _run("append", "--dir", str(run_dir), cwd=run_dir)

        assert done.returncode == 0
        assert "⚠ GAP_LEDGER=unavailable" in done.stdout
        assert "symlinked identity" in done.stdout
        assert stage.read_bytes() == original
        assert not (run_dir / "gaps.jsonl").exists()


class TestCarry:
    """A rerun on an identical head carries the earlier gaps instead of restating them."""

    def _first_run(self, tmp_path: Path, root: Path) -> None:
        """Record one open gap in run-001."""
        first = _review_dir(tmp_path, "run-001")
        _prepare_review(first, root, _watch(tmp_path, "qa\t/x\t1800\n"))
        _stage(first, _gap())
        assert _run("append", "--dir", str(first), cwd=first).returncode == 0

    def test_identical_head_rerun_carries_from_the_earlier_run(self, tmp_path: Path) -> None:
        """The second run's ledger holds the earlier gap with ``carried_from`` and the run prints it."""
        root = _plugin_root(tmp_path)
        self._first_run(tmp_path, root)
        second = _review_dir(tmp_path, "run-002")
        done = _prepare_review(second, root, _watch(tmp_path, "qa\t/x\t1800\n"))
        assert "GAP_CARRY=carried 1 from pr-12-run-001" in done.stdout
        assert "GAP_CARRIED=codex-co-review-quota-exhausted · process ·" in done.stdout
        [carried] = _ledger(second)
        assert carried["carried_from"] == "pr-12-run-001"
        assert carried["run"]["run_id"] == "pr-12-run-002"

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("[" * 10_000 + "]" * 10_000, id="deeply-nested-json"),
            pytest.param("not JSON", id="invalid-json"),
            pytest.param('{"subject":null,"run":{}}', id="invalid-record"),
        ],
    )
    @pytest.mark.parametrize("run_number", ["run-001", "run-002"])
    def test_malformed_ledgers_are_reported(self, tmp_path: Path, content: str, run_number: str) -> None:
        """Prepare names unusable current or prior ledgers and leaves the run running."""
        first = _review_dir(tmp_path, "run-001")
        (first / "gaps.jsonl").write_text(content + "\n", encoding="utf-8")
        run_dir = first if run_number == "run-001" else _review_dir(tmp_path, run_number)

        done = _prepare_review(run_dir, _plugin_root(tmp_path))

        assert done.returncode == 0
        assert not done.stderr
        assert any(line.startswith("⚠ ") and "gaps.jsonl" in line for line in done.stdout.splitlines())

    def test_saved_inventory_supports_closing_gaps_beyond_preview(self, tmp_path: Path) -> None:
        """The bounded preview points to a durable complete inventory, and its final item can close."""
        root = _plugin_root(tmp_path)
        first = _review_dir(tmp_path, "run-001")
        _prepare_review(first, root)
        records = [_gap(f"codex-co-review-role-{index}") for index in range(55)]
        _stage(first, *records)
        assert _run("append", "--dir", str(first), cwd=first).returncode == 0
        second = _review_dir(tmp_path, "run-002")

        prepared = _prepare_review(second, root)

        assert prepared.returncode == 0
        assert f"GAP_CARRIED_LEDGER={second / 'gaps.jsonl'}" in prepared.stdout
        assert prepared.stdout.count("GAP_CARRIED=") == 50
        saved = (second / "gap-prepare.txt").read_text(encoding="utf-8")
        assert saved.count("GAP_CARRIED=") == 55
        last = str(records[-1]["id"])
        assert f"GAP_CARRIED={last} ·" in saved
        _stage(second, _gap(last, status="closed", deduction=None))
        assert _run("append", "--dir", str(second), cwd=second).returncode == 0
        assert gap_ledger.latest_by_id(_ledger(second))[last]["status"] == "closed"
        assert _prepare_review(second, root).returncode == 0
        assert f"GAP_CARRIED={last} ·" not in (second / "gap-prepare.txt").read_text(encoding="utf-8")

    @pytest.mark.parametrize("deduction", [None, 0.0])
    def test_closing_a_carried_gap_is_a_new_event(self, tmp_path: Path, deduction: float | None) -> None:
        """A carried gap this run closed is retracted by a ``status: closed`` record of the same id."""
        root = _plugin_root(tmp_path)
        self._first_run(tmp_path, root)
        second = _review_dir(tmp_path, "run-002")
        _prepare_review(second, root, _watch(tmp_path, "qa\t/x\t1800\n"))
        _stage(second, _gap(status="closed", deduction=deduction))
        assert _run("append", "--dir", str(second), cwd=second).returncode == 0
        carried, closure = _ledger(second)
        assert carried["id"] == closure["id"]
        assert carried.get("status", "open") == "open"
        assert closure["status"] == "closed"
        assert closure["deduction"] == deduction
        assert gap_ledger.latest_by_id([carried, closure]) == {str(carried["id"]): closure}

    def test_second_prepare_does_not_carry_twice(self, tmp_path: Path) -> None:
        """Re-running prepare in the same run lists the carried gaps again without appending them again."""
        root = _plugin_root(tmp_path)
        self._first_run(tmp_path, root)
        second = _review_dir(tmp_path, "run-002")
        _prepare_review(second, root, _watch(tmp_path, "qa\t/x\t1800\n"))
        again = _prepare_review(second, root)
        assert "GAP_CARRY=done earlier in this run" in again.stdout
        assert "GAP_CARRIED=codex-co-review-quota-exhausted" in again.stdout
        assert len(_ledger(second)) == 1

    @pytest.mark.parametrize(
        ("head", "version", "reason"),
        [
            pytest.param(_OTHER_HEAD, "0.69.0", "head differs", id="new-head"),
            pytest.param(_HEAD, "0.70.0", "plugin version differs", id="new-plugin-version"),
        ],
    )
    def test_changed_head_or_version_records_fresh(self, tmp_path: Path, head: str, version: str, reason: str) -> None:
        """A different head or plugin version carries nothing, names why, and exits 0."""
        self._first_run(tmp_path, _plugin_root(tmp_path))
        second = _review_dir(tmp_path, "run-002", head=head)
        done = _prepare_review(second, _plugin_root(tmp_path, version))
        assert done.returncode == 0
        assert f"GAP_CARRY=fresh ({reason})" in done.stdout
        assert not (second / "gaps.jsonl").exists()

    def test_run_numbers_sort_numerically(self, tmp_path: Path) -> None:
        """``run-010`` is newer than ``run-009``; the carry takes the newest earlier run."""
        root = _plugin_root(tmp_path)
        for run in ("run-009", "run-010"):
            run_dir = _review_dir(tmp_path, run)
            _prepare_review(run_dir, root)
            _stage(run_dir, _gap(summary=f"gap seen in {run}"))
            assert _run("append", "--dir", str(run_dir), cwd=run_dir).returncode == 0
        latest = _review_dir(tmp_path, "run-011")
        done = _prepare_review(latest, root)
        assert "carried 1 from pr-12-run-010" in done.stdout


@pytest.mark.skipif(_GIT is None, reason="known limits are found through the repository's main worktree")
class TestKnownLimits:
    """Listed absent hardware classes are printed and enforced as shown once, never deducted again."""

    def _repo_with_limit(self, tmp_path: Path, listed_repo: str = "octo/demo") -> Path:
        """Create a git repository listing ``cuda-absent`` in its known-limits file."""
        repo = tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run([_GIT, "init", "-q"], cwd=repo, check=True)
        (repo / ".notes").mkdir()
        limits = {
            "schema": "known-limits",
            "schema_version": 1,
            "repo": listed_repo,
            "limits": [
                {
                    "id": "cuda-absent",
                    "class": "hardware-absent",
                    "hardware_class": "cuda",
                    "reason": "This host has no NVIDIA GPU.",
                    "host_class": {"os": platform.system().casefold(), "architecture": _ARCHITECTURE},
                },
            ],
        }
        (repo / ".notes" / "known-limits.json").write_text(json.dumps(limits), encoding="utf-8")
        return repo

    def _cuda_gap(self, **extra: object) -> dict[str, object]:
        """Return the hardware-absent gap matching the listed limit."""
        return (
            _gap(
                "cuda-absent",
                **{"class": "hardware-absent", "owner": "host", "hardware_class": "cuda"},
                reason="This host has no NVIDIA GPU.",
                summary="CUDA paths not executed",
                closable_by="run on a host with an NVIDIA GPU",
                evidence="review-env.json",
            )
            | extra
        )

    def test_prepare_prints_listed_limits(self, tmp_path: Path) -> None:
        """The writer sees each listed limit with its exact reason."""
        self._repo_with_limit(tmp_path)
        run_dir = _review_dir(tmp_path, "run-001")
        done = _prepare_review(run_dir, _plugin_root(tmp_path))
        assert "GAP_KNOWN_LIMIT=cuda-absent · cuda · This host has no NVIDIA GPU." in done.stdout

    def test_invalid_limits_file_is_named_not_silently_ignored(self, tmp_path: Path) -> None:
        """A known-limits file listing a package is refused, and prepare says it was ignored."""
        repo = self._repo_with_limit(tmp_path)
        bad = json.loads((repo / ".notes" / "known-limits.json").read_text(encoding="utf-8"))
        bad["limits"][0]["class"] = "environment-provisionable"
        (repo / ".notes" / "known-limits.json").write_text(json.dumps(bad), encoding="utf-8")
        run_dir = _review_dir(tmp_path, "run-001")
        done = _prepare_review(run_dir, _plugin_root(tmp_path))
        assert done.returncode == 0
        assert "⚠ known-limits file ignored" in done.stdout
        assert "GAP_KNOWN_LIMIT=" not in done.stdout

    def test_listed_limit_staged_with_a_deduction_is_stored_without_one(self, tmp_path: Path) -> None:
        """The emitter applies the listed limit: it is shown once and never deducted again."""
        self._repo_with_limit(tmp_path)
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path))
        _stage(run_dir, self._cuda_gap(known_limit=True))
        done = _run("append", "--dir", str(run_dir), cwd=run_dir)
        assert done.returncode == 0, done.stdout
        record = _ledger(run_dir)[0]
        assert record["known_limit"] is True
        assert record["deduction"] is None

    def test_listed_limit_without_deduction_is_appended(self, tmp_path: Path) -> None:
        """The known-limit record with no deduction lands."""
        self._repo_with_limit(tmp_path)
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path))
        _stage(run_dir, self._cuda_gap(known_limit=True, deduction=None))
        done = _run("append", "--dir", str(run_dir), cwd=run_dir)
        assert done.returncode == 0, done.stdout
        assert _ledger(run_dir)[0]["known_limit"] is True

    @pytest.mark.parametrize(
        "summary",
        [
            "known limit: This host has no NVIDIA GPU.",
            "not reproducible on this host: requires CUDA; host has no CUDA device.",
        ],
    )
    def test_documented_confidence_line_retains_listed_waiver(self, tmp_path: Path, summary: str) -> None:
        """The documented report prefixes do not prevent an otherwise eligible waiver."""
        self._repo_with_limit(tmp_path)
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path))
        _stage(run_dir, self._cuda_gap(summary=summary, deduction=None))

        done = _run("append", "--dir", str(run_dir), cwd=run_dir)

        assert done.returncode == 0, done.stdout
        [record] = _ledger(run_dir)
        assert (record.get("known_limit"), record["deduction"]) == (True, None)

    def test_refused_append_preserves_original_deduction_across_retry(self, tmp_path: Path) -> None:
        """A refusal keeps the original stage bytes and lets a retry use the original deduction."""
        repo = self._repo_with_limit(tmp_path)
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path))
        _stage(run_dir, self._cuda_gap())
        stage = run_dir / "gaps.jsonl.rec"
        original = stage.read_bytes()
        parallel = run_dir / "gaps.jsonl.extra.rec"
        parallel.write_text('{"id":"other","class":"invalid"}\n', encoding="utf-8")
        other = parallel.read_bytes()

        refused = _run("append", "--dir", str(run_dir), cwd=run_dir)

        assert refused.returncode == 1
        assert "GAP_LEDGER=refused" in refused.stdout
        assert (stage.read_bytes(), parallel.read_bytes()) == (original, other)
        assert not (run_dir / "gaps.jsonl").exists()
        parallel.unlink()
        (repo / ".notes" / "known-limits.json").unlink()
        retried = _run("append", "--dir", str(run_dir), cwd=run_dir)
        assert retried.returncode == 0, retried.stdout
        [record] = _ledger(run_dir)
        assert (record.get("known_limit"), record["deduction"]) == (None, 0.05)

    def test_registry_spelling_does_not_hide_a_listed_limit(self, tmp_path: Path) -> None:
        """A registry for ``Octo/Demo`` applies to the review of ``octo/demo``."""
        self._repo_with_limit(tmp_path, listed_repo="Octo/Demo")
        run_dir = _review_dir(tmp_path, "run-001")
        done = _prepare_review(run_dir, _plugin_root(tmp_path))
        assert "GAP_KNOWN_LIMIT=cuda-absent" in done.stdout, done.stdout
        _stage(run_dir, self._cuda_gap())
        assert _run("append", "--dir", str(run_dir), cwd=run_dir).returncode == 0
        record = _ledger(run_dir)[0]
        assert (record["known_limit"], record["deduction"]) == (True, None)

    def test_provisioning_cause_is_refused_as_hardware_despite_a_listed_limit(self, tmp_path: Path) -> None:
        """A CPU-only wheel never counts as hardware absence or qualifies for the listed limit."""
        self._repo_with_limit(tmp_path)
        run_dir = _review_dir(tmp_path, "run-001")
        _prepare_review(run_dir, _plugin_root(tmp_path))
        _stage(run_dir, self._cuda_gap(reason="torch CPU-only wheel installed, CUDA not available."))
        stage = run_dir / "gaps.jsonl.rec"
        original = stage.read_bytes()
        done = _run("append", "--dir", str(run_dir), cwd=run_dir)
        assert done.returncode == 1
        assert "GAP_LEDGER=refused" in done.stdout
        assert not (run_dir / "gaps.jsonl").exists()
        assert stage.read_bytes() == original
        assert "GAP_KNOWN_LIMIT_REGISTERED" not in done.stdout
