"""Exercise known-limits capture and the confidence contract."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest
from _modules import load_module_scoped

PLUGIN = Path(__file__).resolve().parents[2]
HELPER = PLUGIN / "shared" / "run_gap_records.py"
REPO = "octo/demo"
REASON = "This host lacks a local CUDA GPU."
LIMIT_LINE = f"GAP_KNOWN_LIMIT=cuda-absent · cuda · {REASON}"
GAP = f"(-0.00) cuda-absent: {REASON} (known limit)"


@pytest.fixture
def review_run(tmp_path: Path) -> Path:
    """Create a PR run with routing evidence and no promoted result."""
    run = tmp_path / ".reports" / "codex" / "code-review" / "pr-7" / "run-001"
    run.mkdir(parents=True)
    (run / "pr-routing.json").write_text(json.dumps({"base_repo": REPO}), encoding="utf-8")
    return run


def _prepare(run: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the shipped CLI against a run directory and capture its streams."""
    return subprocess.run(
        [sys.executable, str(HELPER), "prepare", "--run", str(run)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )


def _write_limits(root: Path, *, repo: str = REPO, os_name: str | None = None, empty: bool = False) -> Path:
    """Write one repository's known-limits fixture for the selected host."""
    path = root / ".notes" / "known-limits.json"
    path.parent.mkdir(exist_ok=True)
    machine = platform.machine().casefold()
    limit = {
        "id": "cuda-absent",
        "class": "hardware-absent",
        "hardware_class": "cuda",
        "reason": REASON,
        "host_class": {
            "os": os_name or platform.system().casefold(),
            "architecture": {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine),
        },
    }
    path.write_text(
        json.dumps({"schema": "known-limits", "schema_version": 1, "repo": repo, "limits": [] if empty else [limit]}),
        encoding="utf-8",
    )
    return path


def test_prepare_prints_the_invoking_checkouts_matching_limit(review_run: Path, tmp_path: Path) -> None:
    """Prefer the recorded invoking checkout over the parent of the reports directory."""
    checkout = tmp_path / "invoking-checkout"
    checkout.mkdir()
    _write_limits(checkout)
    (review_run / "review-entry.json").write_text(json.dumps({"repository": str(checkout)}), encoding="utf-8")

    result = _prepare(review_run)

    assert (result.returncode, result.stdout.splitlines(), result.stderr) == (0, [LIMIT_LINE], "")


def test_prepare_omits_another_hosts_limit(review_run: Path, tmp_path: Path) -> None:
    """A valid limit for another OS cannot waive a gap on this run."""
    _write_limits(tmp_path, os_name="other-os")

    result = _prepare(review_run)

    assert (result.returncode, result.stdout.splitlines(), result.stderr) == (0, ["GAP_KNOWN_LIMITS=none"], "")


def test_prepare_reports_no_limits_when_file_is_missing(review_run: Path) -> None:
    """Report no known limits when the checkout does not contain a limits file."""
    result = _prepare(review_run)

    assert (result.returncode, result.stdout.splitlines(), result.stderr) == (0, ["GAP_KNOWN_LIMITS=none"], "")


def test_prepare_reports_no_limits_for_an_empty_list(review_run: Path, tmp_path: Path) -> None:
    """Report no known limits for a valid file with no entries."""
    _write_limits(tmp_path, empty=True)

    result = _prepare(review_run)

    assert (result.returncode, result.stdout.splitlines(), result.stderr) == (0, ["GAP_KNOWN_LIMITS=none"], "")


def test_prepare_warns_and_prints_no_limit_for_invalid_json(review_run: Path, tmp_path: Path) -> None:
    """Ignore unreadable JSON while returning a diagnostic and a successful exit."""
    path = _write_limits(tmp_path)
    path.write_text("{", encoding="utf-8")

    result = _prepare(review_run)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == [f"⚠ known-limits file ignored ({path}): not valid JSON"]


def test_prepare_ignores_a_limit_with_a_compound_host_reason(review_run: Path, tmp_path: Path) -> None:
    """A registry with extra assertions in its reason does not announce a waiver."""
    path = _write_limits(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["limits"][0]["reason"] = "This host provides TensorRT remotely and lacks a local CUDA GPU."
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _prepare(review_run)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        f"⚠ known-limits file ignored ({path}): limit 1: reason must directly state a device absence matching hardware_class"
    ]


def test_prepare_warns_and_prints_no_limit_for_another_repository(review_run: Path, tmp_path: Path) -> None:
    """Reject a valid file belonging to a repo other than the run's routed base repo."""
    path = _write_limits(tmp_path, repo="octo/other")
    remediated_pr = review_run / "pr"
    remediated_pr.mkdir()
    (review_run / "pr-routing.json").rename(remediated_pr / "pr-routing.json")

    result = _prepare(review_run)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        f"⚠ known-limits file ignored ({path}): known-limits repository octo/other differs from {REPO}"
    ]


def test_prepare_captures_limits_once_without_rewriting_inputs(review_run: Path, tmp_path: Path) -> None:
    """A repeat prepare shows the first capture even when the live registry changes."""
    registry = _write_limits(tmp_path)
    before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in tmp_path.rglob("*") if path.is_file()}

    result = _prepare(review_run)

    after = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in tmp_path.rglob("*") if path.is_file()}
    snapshot = review_run / "gap-known-limits.json"
    captured = after.pop(snapshot)
    assert (result.returncode, result.stdout.splitlines()) == (0, [LIMIT_LINE])
    assert after == before
    registry.unlink()
    repeated = _prepare(review_run)
    assert (repeated.returncode, repeated.stdout.splitlines()) == (0, [LIMIT_LINE])
    assert (snapshot.stat().st_mtime_ns, snapshot.read_bytes()) == captured


def test_prepare_requires_a_run_directory_argument() -> None:
    """A malformed command line exits with the standard usage error."""
    result = subprocess.run(
        [sys.executable, str(HELPER), "prepare"], capture_output=True, encoding="utf-8", check=False, timeout=30
    )

    assert result.returncode == 2
    assert "--run" in result.stderr


@pytest.mark.parametrize(
    "instruction",
    [
        "code-review/references/findings-and-completion.md",
        "code-remediate/references/completion.md",
    ],
)
def test_confidence_instructions_apply_known_limits_before_scoring(instruction: str) -> None:
    """Both writers prepare limits first and account only for supported current gaps."""
    lines = (PLUGIN / "skills" / instruction).read_text(encoding="utf-8").splitlines()
    prepare = next(line for line in lines if "run_gap_records.py prepare" in line)

    assert "Before writing confidence" in prepare
    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/run_gap_records.py prepare --run <run-directory>" in prepare
    assert "GAP_KNOWN_LIMIT=" in prepare
    assert "exact reason" in prepare
    assert "(-0.00) <id>: <reason> (known limit)" in prepare
    assert "once" in prepare
    assert "Omit limits whose absence the current evidence no longer shows." in prepare
    assert "If the file is ignored, deduct and name each hardware gap once." in prepare


def test_open_zero_deduction_gap_becomes_a_known_hardware_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the printed ID to retain the hardware class when the exact reason names another accelerator."""
    _write_limits(tmp_path)
    records = load_module_scoped("run_gap_records", HELPER, monkeypatch)
    metadata = {
        "confidence_gaps": [GAP],
        "confidence_gap_closures": [
            {"gap": GAP, "status": "unresolved", "rationale": "The same absence was observed."}
        ],
    }

    known, note = records.invoking_known_limits(tmp_path)
    drafts, omitted = records.confidence_gap_drafts(metadata)
    raw = records.to_record(drafts[0], {"subject": {"repo": REPO}, "run": {}})
    (record,) = records.apply_known_limits([raw], known)

    assert note is None
    assert omitted == []
    assert (raw["id"], raw["class"], raw["hardware_class"], raw["deduction"]) == (
        "cuda-absent",
        "hardware-absent",
        "cuda",
        0.0,
    )
    assert (record["known_limit"], record["deduction"], record["reason"]) == (True, None, REASON)
