"""Contract tests for the Code Review run-state stub and terminal-state classifier.

A run that stops before finalization records how far it got in ``run-state.json``; ``result.json`` keeps meaning a
promoted result. The finder still treats such a run as incomplete, so the stub cannot hide an unfinished review behind
an older assessed report.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _modules import load_module_registered, load_module_scoped

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
STATE_PATH = PLUGIN_ROOT / "skills" / "code-review" / "review_run_state.py"
PREPARE_PATH = PLUGIN_ROOT / "skills" / "code-review" / "review_prepare.py"
ROUTING_PATH = PLUGIN_ROOT / "skills" / "code-review" / "review_routing.py"
FINDER_PATH = PLUGIN_ROOT / "shared" / "find-review-report.py"
STATE = load_module_registered("review_run_state", STATE_PATH)
PR_URL = "https://github.com/acme/widgets/pull/123"


def _stub(run: Path) -> dict:
    """Read the written stub."""
    return json.loads((run / "run-state.json").read_text(encoding="utf-8"))


def test_mark_writes_an_incomplete_stub_with_a_cause(tmp_path: Path) -> None:
    """The routing checkpoint leaves a versioned stub naming why no result exists yet."""
    stub = STATE.mark(tmp_path, "routed")
    assert stub == _stub(tmp_path)
    assert stub["schema_version"] == 1
    assert stub["review_status"] == "incomplete"
    assert stub["checkpoint"] == "routed"
    assert "routed" in stub["cause"]
    assert not (tmp_path / "result.json").exists()


def test_mark_only_moves_forward(tmp_path: Path) -> None:
    """A later checkpoint replaces an earlier one, never the reverse."""
    STATE.mark(tmp_path, "reviewers-joined")
    STATE.mark(tmp_path, "routed")
    assert _stub(tmp_path)["checkpoint"] == "reviewers-joined"


def test_mark_leaves_a_promoted_run_untouched(tmp_path: Path) -> None:
    """A graceful finish is final: no stub is written beside a promoted result."""
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    assert STATE.mark(tmp_path, "dispatch-prepared") is None
    assert not (tmp_path / "run-state.json").exists()


def test_mark_rejects_unknown_checkpoints(tmp_path: Path) -> None:
    """Only workflow helpers' checkpoints can be recorded."""
    with pytest.raises(ValueError, match="run-state-checkpoint-invalid"):
        STATE.mark(tmp_path, "gates-run")


@pytest.mark.parametrize(
    ("files", "checkpoint"),
    [
        pytest.param({}, None, id="empty"),
        pytest.param({"pr-target.txt": PR_URL}, "allocated", id="allocated"),
        pytest.param({"diff.patch": ""}, "source-collected", id="collected"),
        pytest.param({"dispatch.json": "{}"}, "dispatch-prepared", id="dispatched"),
        pytest.param({"gates.json": "{}", "review-notes.md": ""}, "gates-run", id="gates"),
        pytest.param({"result.candidate.json": "{}"}, "candidate-written", id="unpromoted"),
    ],
)
def test_classify_names_the_last_checkpoint_of_an_unfinished_run(
    tmp_path: Path, files: dict[str, str], checkpoint: str | None
) -> None:
    """A run without a promoted result is incomplete at the latest checkpoint its artifacts prove."""
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    state = STATE.classify(tmp_path)
    assert state["review_status"] == "incomplete"
    assert state["terminal"] is False
    assert state["checkpoint"] == checkpoint
    assert state["cause"]


def test_classify_uses_the_later_of_stub_and_artifacts(tmp_path: Path) -> None:
    """A stale stub cannot pull the checkpoint back; a missing artifact cannot hide a recorded checkpoint."""
    STATE.mark(tmp_path, "reviewers-joined")
    assert STATE.classify(tmp_path)["checkpoint"] == "reviewers-joined"
    (tmp_path / "gates.json").write_text("{}", encoding="utf-8")
    assert STATE.classify(tmp_path)["checkpoint"] == "gates-run"


@pytest.mark.parametrize(
    ("metadata", "status"),
    [
        pytest.param({"scope": "pr", "review_decision": {"recommendation": "APPROVE"}}, "assessed", id="assessed"),
        pytest.param({"scope": "pr", "review_status": "unavailable"}, "unavailable", id="unavailable"),
        pytest.param({"scope": "pr", "review_status": "closed"}, "closed", id="closed"),
        pytest.param({"scope": "pr"}, "invalid", id="no-disposition"),
    ],
)
def test_classify_reads_a_promoted_result_as_terminal(tmp_path: Path, metadata: dict, status: str) -> None:
    """A promoted result decides the state, whatever stub was left earlier."""
    STATE.mark(tmp_path, "routed")
    (tmp_path / "result.json").write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    state = STATE.classify(tmp_path)
    assert state["review_status"] == status
    assert state["terminal"] is True


def test_classify_cli_prints_one_record_per_run(tmp_path: Path) -> None:
    """The installed CLI classifies several run directories for counts without changing them."""
    first, second = tmp_path / "run-001", tmp_path / "run-002"
    first.mkdir()
    second.mkdir()
    STATE.mark(first, "dispatch-prepared")
    before = sorted(path.name for path in first.iterdir())
    completed = subprocess.run(
        [sys.executable, str(STATE_PATH), "classify", str(first), str(second)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [record["checkpoint"] for record in records] == ["dispatch-prepared", None]
    assert sorted(path.name for path in first.iterdir()) == before


MALFORMED_VALUES = [
    pytest.param(["routed"], id="list"),
    pytest.param({"name": "routed"}, id="object"),
    pytest.param(7, id="number"),
    pytest.param(None, id="null"),
]


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_malformed_stub_reads_as_no_stub(tmp_path: Path, value: object) -> None:
    """A stub whose checkpoint is not a recorded name is ignored, and a later mark replaces it."""
    stub = {"schema_version": 1, "review_status": "incomplete", "checkpoint": value, "cause": "x"}
    (tmp_path / "run-state.json").write_text(json.dumps(stub), encoding="utf-8")

    assert STATE.read_stub(tmp_path) is None
    assert STATE.classify(tmp_path)["stub"] is None
    assert STATE.mark(tmp_path, "routed")["checkpoint"] == "routed"


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_malformed_result_disposition_reads_as_invalid(tmp_path: Path, value: object) -> None:
    """A promoted result whose review status is not a recognized name is terminal and invalid, never a crash."""
    (tmp_path / "result.json").write_text(json.dumps({"metadata": {"review_status": value}}), encoding="utf-8")

    state = STATE.classify(tmp_path)

    assert (state["review_status"], state["terminal"]) == ("invalid", True)


def test_one_damaged_run_never_hides_the_others(tmp_path: Path) -> None:
    """The classify CLI reports every run directory even when an earlier one cannot be read."""
    damaged, intact = tmp_path / "run-001", tmp_path / "run-002"
    damaged.mkdir()
    intact.mkdir()
    stub = {"schema_version": 1, "review_status": "incomplete", "checkpoint": ["routed"], "cause": "x"}
    (damaged / "run-state.json").write_text(json.dumps(stub), encoding="utf-8")
    STATE.mark(intact, "dispatch-prepared")

    completed = subprocess.run(
        [sys.executable, str(STATE_PATH), "classify", str(damaged), str(tmp_path / "absent"), str(intact)],
        capture_output=True,
        text=True,
        check=False,
    )

    records = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [record["run"] for record in records] == [str(damaged), str(tmp_path / "absent"), str(intact)]
    assert records[-1]["checkpoint"] == "dispatch-prepared"


def test_finder_still_reports_a_stubbed_run_as_incomplete(tmp_path: Path) -> None:
    """The stub never makes an unfinished newer run look promoted, so no older assessed report is admitted."""
    reports = tmp_path / "reports"
    older = reports / "pr-123" / "run-001"
    older.mkdir(parents=True)
    identity = json.dumps({"number": 123, "url": PR_URL})
    (older / "pr.json").write_text(identity, encoding="utf-8")
    (older / "result.json").write_text(
        json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}),
        encoding="utf-8",
    )
    newer = reports / "pr-123" / "run-002"
    newer.mkdir()
    (newer / "pr.json").write_text(identity, encoding="utf-8")
    STATE.mark(newer, "dispatch-prepared")
    completed = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--target", PR_URL, "--reports-dir", str(reports)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert completed.returncode == 1
    assert f"matching-review-incomplete:{newer}" in completed.stderr


@pytest.mark.parametrize(
    ("kwargs", "recorded"),
    [
        pytest.param({}, True, id="review"),
        pytest.param({"challenge_only": True}, False, id="challenge-only"),
        pytest.param({"fixture_discovery": True}, False, id="fixture-discovery"),
    ],
)
def test_preparation_records_only_review_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict, recorded: bool
) -> None:
    """Review preparation advances the stub; challenge and fixture-discovery calls leave no stub."""
    prepare_module = load_module_scoped("review_prepare_run_state", PREPARE_PATH, monkeypatch)

    def prepared(out: Path, challenge_only: bool = False, fixture_discovery: bool = False) -> str:
        return "prepared"

    wrapped = prepare_module._records_checkpoint("dispatch-prepared")(prepared)
    assert wrapped(tmp_path, **kwargs) == "prepared"
    assert (tmp_path / "run-state.json").exists() is recorded


def test_stub_write_failure_never_undoes_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A filesystem error writing the diagnostic stub is reported, and the preparation result is still returned."""
    prepare_module = load_module_scoped("review_prepare_run_state_failure", PREPARE_PATH, monkeypatch)

    def refuse(run_dir: Path, checkpoint: str) -> None:
        raise PermissionError("read-only run directory")

    monkeypatch.setattr(prepare_module.review_run_state, "mark", refuse)
    wrapped = prepare_module._records_checkpoint("reviewers-joined")(lambda out: {"roles": 2})
    assert wrapped(tmp_path) == {"roles": 2}
    assert "run-state-unrecorded:reviewers-joined:read-only run directory" in capsys.readouterr().err


def test_refused_stub_target_never_undoes_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run directory the stub writer refuses is reported like a write error, and preparation still succeeds."""
    prepare_module = load_module_scoped("review_prepare_run_state_refused", PREPARE_PATH, monkeypatch)
    wrapped = prepare_module._records_checkpoint("dispatch-prepared")(lambda out: "prepared")

    assert wrapped(tmp_path / "absent") == "prepared"
    assert "run-state-unrecorded:dispatch-prepared:run-state-directory-invalid" in capsys.readouterr().err


def test_routing_succeeds_when_its_stub_target_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Routing reports a stub it cannot record and still prints its artifact, as the stub is a diagnostic."""
    routing = load_module_scoped("review_routing_run_state", ROUTING_PATH, monkeypatch)
    absent = tmp_path / "absent"
    monkeypatch.setattr(routing, "synchronize_routing", lambda out: out / "review-routing.json")
    monkeypatch.setattr(sys, "argv", ["review_routing.py", "--out", str(absent)])

    assert routing.main() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == str(absent / "review-routing.json")
    assert "run-state-unrecorded:routed:run-state-directory-invalid" in captured.err
