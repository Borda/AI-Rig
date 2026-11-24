"""Run the Decide block of ``modes/exec-approval.md`` under bash with stub helpers and assert what it records.

The contract tests parse the block's text; these run it, so loosening the skip rule or the ask-keeping logic in any
spelling fails here: each case asserts the ``exec-plan.txt`` line and the decision word the block leaves behind.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_MODES = Path(__file__).resolve().parents[3] / "skills/resolve/modes"
_HEAD = "a" * 40
_BASH = shutil.which("bash")
_JQ = shutil.which("jq")

_HOOKS_STUB = """\
import json, os, sys
from pathlib import Path
with Path(os.environ["STUB_LOG"]).open("a") as log:
    log.write("hooks " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("STUB_HOOKS_RC", "0") != "0":
    sys.exit(int(os.environ["STUB_HOOKS_RC"]))
print(json.dumps({"decision": os.environ.get("STUB_HOOKS", "run"), "reason": "stub"}))
"""

_GUARD_STUB = """\
import os, sys
from pathlib import Path
with Path(os.environ["STUB_LOG"]).open("a") as log:
    log.write("guard " + " ".join(sys.argv[1:]) + "\\n")
command = sys.argv[1]
if command == "scan":
    if os.environ.get("STUB_SCAN_RC", "0") != "0":
        sys.exit(1)
    out = sys.argv[sys.argv.index("--out") + 1]
    Path(out).write_text("{}")
    print("verdict: clean\\nrisk: none")
elif command == "bind":
    sys.exit(int(os.environ.get("STUB_BIND_RC", "0")))
"""

_CHECK_STUB = """\
import os, sys
from pathlib import Path
with Path(os.environ["STUB_LOG"]).open("a") as log:
    log.write("check " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("STUB_CHECK_RC", "0") != "0":
    sys.exit(int(os.environ["STUB_CHECK_RC"]))
sys.stdout.reconfigure(newline="\\n")  # the real helper's contract: a bare line feed, pinned in test_code_safety_status
print(os.environ.get("STUB_CHECK", "ask reason=stub"))
"""

_ROOT_STUB = """\
import os
print(os.environ["STUB_PLUGIN_ROOT"])
"""

# A shell function, not a symlink: a venv launcher resolves its ``pyvenv.cfg`` from where it is invoked, so a symlink to
# it fails on Windows (``failed to locate pyvenv.cfg``).
_PYTHON_FUNCTION = f'python() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'

# Native Windows jq ends every output line with CRLF unless ``-b`` is passed; modelling it on every host makes a jq call
# that omits ``-b`` fail here as it does on Windows, where the stray carriage return turns ``run`` into an unknown word.
_JQ_CRLF = r"""jq() {
    for option in "$@"; do
        if [ "$option" = "-b" ] || [ "$option" = "--binary" ]; then
            command jq "$@"
            return
        fi
    done
    command jq "$@" | sed 's/$/\r/'
}
"""

pytestmark = pytest.mark.skipif(_BASH is None or _JQ is None, reason="needs bash and jq")


def _decide_block() -> str:
    """Return the Decide fence of ``exec-approval.md`` as one script."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in (_MODES / "exec-approval.md").read_text(encoding="utf-8").splitlines():
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip() == "```":
            blocks.append(current)
            current = None
        else:
            current.append(line)
    found = [block for block in blocks if any("exec-plan.txt" in line for line in block)]
    assert len(found) == 1
    return "\n".join(found[0]) + "\n"


def _bash(
    world: Path, script: str, environ: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` under bash from a file, never through ``-c``.

    Windows caps a command line near 8191 characters and cuts a longer one mid-string, so a block that grew past that
    stops with an unmatched quote there while passing on Linux and macOS.
    """
    path = world / "run-script.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run([_BASH, path.as_posix()], env=environ, capture_output=True, text=True, check=False, cwd=cwd)


@dataclass
class Run:
    """The files a Decide run left in the implementation directory, plus the helper calls it made."""

    plan: str
    decision: str | None
    log: list[str]
    impl: Path

    def called(self, helper: str) -> list[str]:
        """Return the logged calls of one stub helper."""
        return [line for line in self.log if line.startswith(helper + " ")]


@pytest.fixture
def world(tmp_path: Path) -> Path:
    """Return a temp dir holding stub helpers, session sentinels and an implementation directory."""
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    for name, body in (
        ("resolve_commit_hooks.py", _HOOKS_STUB),
        ("untrusted_code_guard.py", _GUARD_STUB),
        ("code_safety_status.py", _CHECK_STUB),
        ("resolve_plugin_root.py", _ROOT_STUB),
    ):
        (plugin / "bin" / name).write_text(body, encoding="utf-8", newline="\n")
    (tmp_path / "tmp").mkdir()
    (tmp_path / "impl").mkdir()
    for key, value in (
        ("impl-dir", str(tmp_path / "impl")),
        ("pr-number", "42"),
        ("pr-head-oid", _HEAD),
        ("base-ref", "main"),
        ("is-cross-repo", "true"),
        ("report-file", "/reports/pr42/review.md"),
    ):
        (tmp_path / "tmp" / f"resolve-{key}-t1").write_text(value + "\n", encoding="utf-8", newline="\n")
    return tmp_path


def _run(world: Path, **env: str) -> Run:
    """Run the Decide block once with the given stub behaviour and return what it recorded."""
    impl = world / "impl"
    environ = {
        "PATH": os.environ["PATH"],
        "CLAUDE_PLUGIN_ROOT": str(world / "plugin"),
        "STUB_PLUGIN_ROOT": str(world / "plugin"),
        "STUB_LOG": str(world / "stub.log"),
        "TMPDIR": str(world / "tmp"),
        "CLAUDE_CODE_SESSION_ID": "t1",
        **env,
    }
    done = _bash(world, _PYTHON_FUNCTION + _JQ_CRLF + _decide_block(), environ)
    assert done.returncode == 0, done.stdout + done.stderr
    decision_file = impl / "pr-code-decision.txt"
    log_file = world / "stub.log"
    return Run(
        plan=(impl / "exec-plan.txt").read_text(encoding="utf-8").strip(),
        decision=decision_file.read_text(encoding="utf-8") if decision_file.exists() else None,
        log=log_file.read_text(encoding="utf-8").splitlines() if log_file.exists() else [],
        impl=impl,
    )


def test_auto_only_when_the_check_says_auto_and_binds_the_sandbox_tier(world: Path) -> None:
    """A clean check binds the default tier, never the unsandboxed decision, and records ``auto``."""
    run = _run(world, STUB_CHECK="auto")
    assert run.plan == "pr-code=auto hooks=run"
    assert run.decision == "auto\n"
    (check,) = run.called("check")
    assert "--no-sandbox-needed" not in check
    (bind,) = [line for line in run.called("guard") if line.startswith("guard bind")]
    assert "--scope run" in bind
    assert not any(flag in bind for flag in ("--tier", "--decision", "unsandboxed"))


@pytest.mark.parametrize(
    "check",
    [
        pytest.param("ask reason=findings-present", id="a-finding"),
        pytest.param("ask reason=head-mismatch", id="moved-head"),
        pytest.param("ask reason=no-review-status", id="no-review-record"),
        pytest.param("", id="check-prints-nothing"),
    ],
)
def test_anything_but_clean_scans_asks_and_binds_nothing(world: Path, check: str) -> None:
    """A finding, a moved head or a missing review record asks the question and never binds the unsandboxed tier."""
    run = _run(world, STUB_CHECK=check)
    assert run.plan == "pr-code=ask hooks=run"
    assert run.decision is None
    assert not [line for line in run.called("guard") if line.startswith("guard bind")]


@pytest.mark.parametrize(
    "check",
    [
        pytest.param("ask reason=head-mismatch", id="check-says-ask"),
        pytest.param("auto extra", id="check-adds-words"),
        pytest.param("", id="check-prints-nothing"),
    ],
)
def test_anything_but_the_exact_word_auto_asks(world: Path, check: str) -> None:
    """Only the exact word ``auto`` skips the question; any other output asks and records no decision."""
    run = _run(world, STUB_CHECK=check)
    assert run.plan == "pr-code=ask hooks=run"
    assert run.decision is None
    assert not [line for line in run.called("guard") if line.startswith("guard bind")]


def test_a_crashed_check_asks(world: Path) -> None:
    """A check that fails without output leaves nothing to read, and that must ask rather than skip."""
    run = _run(world, STUB_CHECK_RC="2")
    assert run.plan == "pr-code=ask hooks=run"
    assert run.decision is None


def test_a_failed_bind_asks_and_records_nothing(world: Path) -> None:
    """When the approval cannot be bound the question stays and no ``auto`` is written."""
    run = _run(world, STUB_CHECK="auto", STUB_BIND_RC="1")
    assert run.plan == "pr-code=ask hooks=run"
    assert run.decision is None


def test_a_failed_scan_asks_with_an_unavailable_note(world: Path) -> None:
    """No fresh scan means no skip: the question is asked and its evidence says the scan was unavailable."""
    run = _run(world, STUB_SCAN_RC="1", STUB_CHECK="auto")
    assert run.plan == "pr-code=ask hooks=run"
    assert "scan unavailable" in (run.impl / "code-scan.txt").read_text(encoding="utf-8")
    assert not run.called("check")


def test_a_report_path_without_a_directory_never_reads_a_sidecar(world: Path) -> None:
    """An empty or bare ``REPORT_FILE`` cannot name the PR's report directory, so the check is not even run."""
    (world / "tmp" / "resolve-report-file-t1").write_text("review.md\n", encoding="utf-8", newline="\n")
    run = _run(world, STUB_CHECK="auto")
    assert run.plan == "pr-code=ask hooks=run"
    assert not run.called("check")


def test_the_sidecar_is_read_beside_the_report(world: Path) -> None:
    """The status file is the one next to the report named by the sentinel."""
    run = _run(world, STUB_CHECK="ask reason=x")
    (check,) = run.called("check")
    # an MSYS bash hands a leading-slash argument to a native program as a drive path, so only the tail is compared
    assert check.replace("\\", "/").split("--status ")[1].split(" --scan")[0].endswith("/reports/pr42/code-safety.json")


def test_a_missing_cross_repo_sentinel_is_treated_as_a_fork(world: Path) -> None:
    """With no answer on origin the gate stays: the scan runs and the question is asked."""
    (world / "tmp" / "resolve-is-cross-repo-t1").unlink()
    run = _run(world, STUB_CHECK="ask reason=x")
    assert run.plan == "pr-code=ask hooks=run"
    assert run.called("check")


def test_a_same_repo_pr_has_no_guard_and_no_question(world: Path) -> None:
    """Only the recorded answer ``false`` skips the gate, and it scans nothing."""
    (world / "tmp" / "resolve-is-cross-repo-t1").write_text("false\n", encoding="utf-8", newline="\n")
    run = _run(world, STUB_CHECK="auto")
    assert run.plan == "pr-code=same-repo hooks=run"
    assert not run.called("guard")
    assert not run.called("check")


@pytest.mark.parametrize(
    ("content", "approval", "expected"),
    [
        pytest.param("auto\n", True, "recorded", id="auto"),
        pytest.param("sandbox\n", True, "recorded", id="sandbox"),
        pytest.param("unsandboxed\n", True, "recorded", id="unsandboxed"),
        pytest.param("auto\n", False, "ask", id="auto-without-approval-file"),
        pytest.param("sandbox\n", False, "ask", id="sandbox-without-approval-file"),
        pytest.param("unsandboxed\n", False, "ask", id="unsandboxed-without-approval-file"),
        pytest.param("deny\n", False, "recorded", id="deny"),
        pytest.param("deny", False, "recorded", id="deny-without-newline"),
        pytest.param("", False, "recorded", id="empty-file-fails-closed"),
        pytest.param("auto", True, "recorded", id="auto-without-newline"),
        pytest.param("sandbox", True, "recorded", id="sandbox-without-newline"),
        pytest.param("unsandboxed", True, "recorded", id="unsandboxed-without-newline"),
        pytest.param("stale\n", True, "ask", id="stale-asks-again"),
        pytest.param("stale", True, "ask", id="stale-without-newline-asks-again"),
        pytest.param("maybe\n", True, "ask", id="unknown-word-asks"),
    ],
)
def test_recorded_decision_words(world: Path, content: str, approval: bool, expected: str) -> None:
    """A word with its approval file, or deny, is never rescanned; any other state is decided again like the prelude
    does."""
    (world / "impl" / "pr-code-decision.txt").write_text(content, encoding="utf-8", newline="\n")
    if approval:
        (world / "impl" / "code-approval.txt").write_text("approval\n", encoding="utf-8", newline="\n")
    run = _run(world, STUB_CHECK="ask reason=stub")
    assert run.plan == f"pr-code={expected} hooks=run"
    assert run.decision == content
    assert bool(run.called("guard")) is (expected == "ask")


def test_a_lost_sentinel_does_not_erase_an_earlier_hook_ask(world: Path) -> None:
    """A run that loses its PR number after a hook question was found necessary keeps that question for the prelude."""
    (world / "impl" / "exec-plan.txt").write_text("pr-code=ask hooks=ask\n", encoding="utf-8", newline="\n")
    (world / "impl" / "hooks-inspection.json").write_text('{"decision": "ask"}\n', encoding="utf-8", newline="\n")
    (world / "tmp" / "resolve-pr-number-t1").write_text("\n", encoding="utf-8", newline="\n")
    run = _run(world)
    assert run.plan == "pr-code=deferred hooks=ask"
    assert _prelude_check(world) == "hooks=ask-kept"


def test_a_lost_sentinel_without_an_earlier_ask_defers_both(world: Path) -> None:
    """The earlier ask is kept only with its saved inspection; an ask without the text defers like a first run."""
    (world / "impl" / "exec-plan.txt").write_text("pr-code=ask hooks=ask\n", encoding="utf-8", newline="\n")
    (world / "tmp" / "resolve-pr-head-oid-t1").write_text("\n", encoding="utf-8", newline="\n")
    run = _run(world)
    assert run.plan == "pr-code=deferred hooks=deferred"


def test_a_recorded_deny_is_not_overridden_by_a_clean_check(world: Path) -> None:
    """The refusal outlives a rerun that would otherwise skip the question."""
    (world / "impl" / "pr-code-decision.txt").write_text("deny", encoding="utf-8", newline="\n")
    run = _run(world, STUB_CHECK="auto")
    assert run.decision == "deny"
    assert not run.called("check")


def test_an_earlier_pr_code_ask_is_kept_not_rescanned_into_a_skip(world: Path) -> None:
    """A question found necessary at Step 4 stays one: the scan and the check do not run again."""
    (world / "impl" / "exec-plan.txt").write_text("pr-code=ask hooks=run\n", encoding="utf-8", newline="\n")
    (world / "impl" / "code-scan.txt").write_text("verdict: x\n", encoding="utf-8", newline="\n")
    run = _run(world, STUB_CHECK="auto")
    assert run.plan == "pr-code=ask hooks=run"
    assert run.decision is None
    assert not run.called("guard")
    assert not run.called("check")


def test_a_stale_pr_code_ask_without_evidence_is_decided_afresh(world: Path) -> None:
    """The earlier ask is only kept together with the scan evidence it showed."""
    (world / "impl" / "exec-plan.txt").write_text("pr-code=ask hooks=run\n", encoding="utf-8", newline="\n")
    run = _run(world, STUB_CHECK="auto")
    assert run.plan == "pr-code=auto hooks=run"


def _earlier_hook_ask(world: Path) -> Path:
    """Write the state a Step 4 hook question leaves and return the saved inspection file."""
    (world / "impl" / "exec-plan.txt").write_text("pr-code=ask hooks=ask\n", encoding="utf-8", newline="\n")
    inspection = world / "impl" / "hooks-inspection.json"
    inspection.write_text('{"decision": "ask", "reason": "earlier"}\n', encoding="utf-8", newline="\n")
    (world / "impl" / "code-scan.txt").write_text("verdict: x\n", encoding="utf-8", newline="\n")
    return inspection


def _note(world: Path) -> str:
    """Return the note the hook question quotes, or an empty string when the block wrote none."""
    note = world / "impl" / "hooks-inspection-note.txt"
    return note.read_text(encoding="utf-8").strip() if note.exists() else ""


def test_an_earlier_hook_ask_is_never_loosened_into_an_unasked_run(world: Path) -> None:
    """A hook question found necessary at Step 4 stays one when deciding again says ``run``, and it shows the inspection
    of the staged merge tree, so the digest the user approves is the one the merge commit checks."""
    inspection = _earlier_hook_ask(world)
    run = _run(world, STUB_HOOKS="run")
    assert run.plan == "pr-code=ask hooks=ask"
    assert json.loads(inspection.read_text(encoding="utf-8"))["reason"] == "stub"
    assert not (run.impl / "approved-hooks.txt").exists()
    assert _note(world) == ""


def test_an_earlier_hook_ask_shows_the_digest_of_the_merged_tree(world: Path) -> None:
    """The question the user answers shows the inspection taken after the merge was staged, so the approved digest is
    the one the merge commit checks."""
    inspection = _earlier_hook_ask(world)
    run = _run(world, STUB_HOOKS="ask")
    (call,) = run.called("hooks")
    assert f"--head {_HEAD}" in call
    assert run.plan == "pr-code=ask hooks=ask"
    assert json.loads(inspection.read_text(encoding="utf-8"))["reason"] == "stub"
    assert _note(world) == ""


@pytest.mark.parametrize("decision", ["none", "disabled"])
def test_an_earlier_hook_ask_is_dropped_when_no_hook_would_run(world: Path, decision: str) -> None:
    """No installed hook, or a saved never policy, leaves nothing to approve: the question is dropped and hooks stay
    disabled, recorded the way a first decision records it."""
    inspection = _earlier_hook_ask(world)
    run = _run(world, STUB_HOOKS=decision)
    assert run.plan == f"pr-code=ask hooks={decision}"
    assert (run.impl / "approved-hooks.txt").read_text(encoding="utf-8") == ""
    assert (run.impl / "hooks-decision.txt").read_text(encoding="utf-8") == f"{decision}\n"
    assert json.loads(inspection.read_text(encoding="utf-8"))["decision"] == decision


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"STUB_HOOKS_RC": "3"}, id="helper-fails"),
        pytest.param({"STUB_HOOKS": "maybe"}, id="unknown-decision"),
    ],
)
def test_a_failed_re_decision_keeps_the_ask_and_says_its_digest_predates_the_merge(
    world: Path, env: dict[str, str]
) -> None:
    """When the merged tree cannot be inspected the earlier question stays, and a note tells the user its digest was
    taken before the merge, since an approval of it no longer matches once the merge changes the hooks."""
    inspection = _earlier_hook_ask(world)
    run = _run(world, **env)
    assert run.plan == "pr-code=ask hooks=ask"
    assert json.loads(inspection.read_text(encoding="utf-8"))["reason"] == "earlier"
    assert not (run.impl / "approved-hooks.txt").exists()
    assert "before the base merge was staged" in _note(world)


def test_a_later_successful_re_decision_clears_the_note(world: Path) -> None:
    """A note from a failed pass never outlives the inspection it described."""
    _earlier_hook_ask(world)
    _run(world, STUB_HOOKS_RC="3")
    assert _note(world)
    _run(world, STUB_HOOKS="ask")
    assert _note(world) == ""


def test_a_helper_made_run_decision_refreshes_its_digest_after_the_merge(world: Path) -> None:
    """The helper's own ``run`` is bound to the merged tree, or the merge commit is refused for a base-side edit."""
    (world / "impl" / "approved-hooks.txt").write_text("0" * 64 + "\n", encoding="utf-8", newline="\n")
    (world / "impl" / "hooks-decision.txt").write_text("run\n", encoding="utf-8", newline="\n")
    run = _run(world, STUB_HOOKS="run")
    (call,) = run.called("hooks")
    assert f"--head {_HEAD}" in call
    assert f"--approved-file {(world / 'impl' / 'approved-hooks.txt').as_posix()}" in call.replace("\\", "/")
    assert run.plan.endswith("hooks=recorded")


def test_a_users_hook_approval_is_never_rewritten(world: Path) -> None:
    """An approval the user gave (the helper had asked) is not recomputed on a later pass."""
    (world / "impl" / "approved-hooks.txt").write_text("0" * 64 + "\n", encoding="utf-8", newline="\n")
    (world / "impl" / "hooks-decision.txt").write_text("ask\n", encoding="utf-8", newline="\n")
    run = _run(world, STUB_HOOKS="run")
    assert not run.called("hooks")
    assert run.plan.endswith("hooks=recorded")


def test_the_hook_decision_compares_the_pr_head(world: Path) -> None:
    """The helper is asked about the PR head, so a staged base merge is not counted as the PR's change."""
    run = _run(world, STUB_HOOKS="ask")
    (call,) = run.called("hooks")
    assert f"--head {_HEAD}" in call
    assert "--base origin/main" in call
    assert run.plan.endswith("hooks=ask")


@pytest.mark.parametrize("decision", ["none", "run", "disabled"])
def test_hook_decisions_that_need_no_question(world: Path, decision: str) -> None:
    """The helper's answer is recorded verbatim when it needs no question."""
    run = _run(world, STUB_HOOKS=decision)
    assert run.plan.endswith(f"hooks={decision}")


def test_a_failed_hook_inspection_disables_hooks_with_an_empty_approval(world: Path) -> None:
    """When the helper fails no digest is invented: the approval file exists and is empty, hooks stay disabled."""
    run = _run(world, STUB_HOOKS_RC="3")
    assert run.plan.endswith("hooks=disabled")
    assert (run.impl / "approved-hooks.txt").read_text(encoding="utf-8") == ""


def test_a_recorded_hook_choice_is_not_decided_again(world: Path) -> None:
    """The approval file existing, even empty, means the choice is made."""
    (world / "impl" / "approved-hooks.txt").write_text("", encoding="utf-8", newline="\n")
    run = _run(world, STUB_HOOKS="ask")
    assert run.plan.endswith("hooks=recorded")
    assert not run.called("hooks")


def _prelude_check(world: Path) -> str:
    """Run the Step 8 recovery check fence of ``dispatch-prelude.md`` and return what it prints."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in (_MODES / "dispatch-prelude.md").read_text(encoding="utf-8").splitlines():
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip() == "```":
            blocks.append(current)
            current = None
        else:
            current.append(line)
    (block,) = [b for b in blocks if any("hooks=ask-kept" in line for line in b)]
    environ = {"PATH": os.environ["PATH"], "TMPDIR": str(world / "tmp"), "CLAUDE_CODE_SESSION_ID": "t1"}
    done = subprocess.run(
        [_BASH, "-c", "\n".join(block) + "\n"], env=environ, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize(
    ("plan", "inspection", "expected"),
    [
        pytest.param("pr-code=ask hooks=ask", '{"decision": "ask"}', "hooks=ask-kept", id="ask-with-inspection"),
        pytest.param("pr-code=auto hooks=ask", '{"decision": "ask"}', "hooks=ask-kept", id="ask-after-auto"),
        pytest.param("pr-code=ask hooks=run", '{"decision": "run"}', "hooks=decide", id="no-ask-recorded"),
        pytest.param("pr-code=ask hooks=ask", "", "hooks=decide", id="ask-without-inspection-text"),
        pytest.param("pr-code=ask hooks=ask", None, "hooks=decide", id="ask-without-inspection-file"),
        pytest.param("pr-code=deferred hooks=deferred", '{"decision": "ask"}', "hooks=decide", id="deferred-plan"),
        pytest.param(None, '{"decision": "ask"}', "hooks=decide", id="no-plan-file"),
    ],
)
def test_prelude_recovery_keeps_an_earlier_hook_ask_only_with_its_inspection(
    world: Path, plan: str | None, inspection: str | None, expected: str
) -> None:
    """The Step 8 recovery asks from the saved inspection only when both the earlier ask and its text exist."""
    if plan is not None:
        (world / "impl" / "exec-plan.txt").write_text(plan + "\n", encoding="utf-8", newline="\n")
    if inspection is not None:
        (world / "impl" / "hooks-inspection.json").write_text(inspection, encoding="utf-8", newline="\n")
    assert _prelude_check(world) == expected


def test_a_run_without_a_pr_defers_both_to_the_prelude(world: Path) -> None:
    """With no PR number nothing is decided here; the Step 8 prelude decides."""
    (world / "tmp" / "resolve-pr-number-t1").write_text("\n", encoding="utf-8", newline="\n")
    run = _run(world)
    assert run.plan == "pr-code=deferred hooks=deferred"
    assert not run.log


def _answer_block(marker: str) -> str:
    """Return the one fixed answer fence of ``exec-approval.md`` that contains ``marker``."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in (_MODES / "exec-approval.md").read_text(encoding="utf-8").splitlines():
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip() == "```":
            blocks.append(current)
            current = None
        else:
            current.append(line)
    # the stale block also writes the ``sandbox`` word when a clean rescan renews an approval: answers never read HEAD
    (found,) = [
        block
        for block in blocks
        if any(marker in line for line in block)
        and (marker == "echo stale >" or not any("git rev-parse" in line for line in block))
        and not any("exec-plan.txt" in line for line in block)  # the Decide block records words too
    ]
    return "\n".join(found) + "\n"


def _checkout(world: Path) -> Path:
    """Return a throw-away Git checkout with one commit, so a block that reads ``HEAD`` never depends on the caller's
    cwd."""
    repo = world / "checkout"
    if not repo.exists():
        repo.mkdir()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "t"],
            ["commit", "-q", "--allow-empty", "-m", "base"],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _head(repo: Path) -> str:
    """Return the checkout's current commit id."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _block_env(world: Path, **env: str) -> dict[str, str]:
    """Return the environment an answer block runs in: stub plugin root and helpers, session and temp sentinels."""
    return {
        "PATH": os.environ["PATH"],
        "CLAUDE_PLUGIN_ROOT": str(world / "plugin"),
        "STUB_PLUGIN_ROOT": str(world / "plugin"),
        "STUB_LOG": str(world / "stub.log"),
        "TMPDIR": str(world / "tmp"),
        "CLAUDE_CODE_SESSION_ID": "t1",
        **env,
    }


def _stale_block(world: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the ``PR code = stale`` block once in the throw-away checkout."""
    environ = _block_env(world, **env)
    return _bash(world, _PYTHON_FUNCTION + _JQ_CRLF + _answer_block("echo stale >"), environ, cwd=_checkout(world))


def test_the_first_stale_refusal_saves_the_current_commit_and_asks_again(world: Path) -> None:
    """The first refusal records ``stale`` plus the checkout's current commit for the new scan and bind."""
    impl = world / "impl"
    (impl / "code-approval.txt").write_text("approval\n", encoding="utf-8", newline="\n")
    done = _stale_block(world)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (impl / "pr-code-decision.txt").read_text(encoding="utf-8") == "stale\n"
    assert (impl / "pr-code-rebind-head.txt").read_text(encoding="utf-8").strip() == _head(_checkout(world))


def test_a_second_refusal_at_the_same_commit_is_final(world: Path) -> None:
    """A committed rebind cannot cover uncommitted work, so the same commit refusing again is a reported gap."""
    impl = world / "impl"
    (impl / "pr-code-rebind-head.txt").write_text(_head(_checkout(world)) + "\n", encoding="utf-8", newline="\n")
    (impl / "code-approval.txt").write_text("approval\n", encoding="utf-8", newline="\n")
    done = _stale_block(world)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (impl / "pr-code-decision.txt").read_text(encoding="utf-8") == "deny\n"
    assert not (impl / "code-approval.txt").exists()
    assert "tests not run" in done.stdout


def test_a_refusal_after_the_commit_moved_asks_again(world: Path) -> None:
    """Work committed since the last rebind is new ground, so the question is asked once more at the new commit."""
    impl = world / "impl"
    repo = _checkout(world)
    (impl / "pr-code-rebind-head.txt").write_text(_head(repo) + "\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "more"], cwd=repo, check=True, capture_output=True)
    done = _stale_block(world)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (impl / "pr-code-decision.txt").read_text(encoding="utf-8") == "stale\n"
    assert (impl / "pr-code-rebind-head.txt").read_text(encoding="utf-8").strip() == _head(repo)


@pytest.mark.parametrize(
    ("marker", "word"),
    [
        pytest.param("# runners read only the approval file, so a declined approval", "deny", id="do-not-run"),
        pytest.param("echo stale >", "stale", id="stale-refusal"),
    ],
)
def test_declining_or_staling_an_approval_sets_the_approval_file_aside(world: Path, marker: str, word: str) -> None:
    """A decision word that is not an approval never sits beside a usable ``code-approval.txt``."""
    impl = world / "impl"
    (impl / "code-approval.txt").write_text("approval\n", encoding="utf-8", newline="\n")
    environ = _block_env(world)
    done = _bash(world, _PYTHON_FUNCTION + _answer_block(marker), environ, cwd=_checkout(world))
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (impl / "code-approval.txt").exists()
    assert (impl / "code-approval.set-aside").read_text(encoding="utf-8") == "approval\n"
    assert (impl / "pr-code-decision.txt").read_text(encoding="utf-8") == word + "\n"


@pytest.mark.parametrize(
    ("marker", "word"),
    [
        pytest.param("# runners read only the approval file, so a declined approval", "deny", id="do-not-run"),
        pytest.param("echo stale >", "stale", id="stale-refusal"),
    ],
)
def test_a_failed_set_aside_blocks_instead_of_leaving_a_usable_approval(world: Path, marker: str, word: str) -> None:
    """When the approval file cannot be moved away the block stops, and no decision word is written beside it."""
    impl = world / "impl"
    (impl / "code-approval.txt").write_text("approval\n", encoding="utf-8", newline="\n")
    # Git-style mv cannot replace a non-empty directory with a file, which makes the set-aside fail
    (impl / "code-approval.set-aside" / "code-approval.txt").mkdir(parents=True)
    (impl / "code-approval.set-aside" / "code-approval.txt" / "occupied").write_text(
        "x", encoding="utf-8", newline="\n"
    )
    environ = _block_env(world)
    done = _bash(world, _PYTHON_FUNCTION + _answer_block(marker), environ, cwd=_checkout(world))
    assert done.returncode == 1
    assert "BLOCKED" in done.stdout
    assert (impl / "code-approval.txt").exists()
    assert not (impl / "pr-code-decision.txt").exists()


@pytest.mark.parametrize(
    ("marker", "extra"),
    [
        pytest.param("echo sandbox >", [], id="sandbox"),
        pytest.param("echo unsandboxed >", ["--tier env-only --decision unsandboxed"], id="unsandboxed"),
    ],
)
@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        pytest.param("b" * 40 + "\n", "b" * 40, id="after-a-stale-refusal"),
        pytest.param(None, _HEAD, id="ordinary-answer"),
    ],
)
def test_an_answer_binds_the_commit_a_stale_refusal_saved(
    world: Path, marker: str, extra: list[str], saved: str | None, expected: str
) -> None:
    """After a stale refusal the answer binds the saved commit; otherwise it binds the PR head."""
    impl = world / "impl"
    if saved is not None:
        (impl / "pr-code-rebind-head.txt").write_text(saved, encoding="utf-8", newline="\n")
    environ = {
        "PATH": os.environ["PATH"],
        "CLAUDE_PLUGIN_ROOT": str(world / "plugin"),
        "STUB_PLUGIN_ROOT": str(world / "plugin"),
        "STUB_LOG": str(world / "stub.log"),
        "TMPDIR": str(world / "tmp"),
        "CLAUDE_CODE_SESSION_ID": "t1",
    }
    done = _bash(world, _PYTHON_FUNCTION + _answer_block(marker), environ)
    assert done.returncode == 0, done.stdout + done.stderr
    (bind,) = [
        line for line in (world / "stub.log").read_text(encoding="utf-8").splitlines() if line.startswith("guard bind")
    ]
    assert f"--head {expected} " in bind
    assert all(flag in bind for flag in extra)


def test_a_stale_word_never_rebinds_on_a_clean_check(world: Path) -> None:
    """A refused approval asks again even when both scans are clean."""
    (world / "impl" / "pr-code-decision.txt").write_text("stale\n", encoding="utf-8", newline="\n")
    run = _run(world, STUB_CHECK="auto")
    assert run.plan == "pr-code=ask hooks=run"
    assert not [line for line in run.called("guard") if line.startswith("guard bind")]
    assert run.decision == "stale\n"


def test_a_stale_refusal_from_any_consumer_runs_the_stale_block() -> None:
    """The full-suite gate and the prelude both route an exit-3 refusal through the stale block, not a bare word
    write."""
    gate = (_MODES / "lint-qa-gate.md").read_text(encoding="utf-8")
    prelude = (_MODES / "dispatch-prelude.md").read_text(encoding="utf-8")
    assert "run the `PR code = stale` block of `exec-approval.md`" in gate
    assert "the parent runs the `PR code = stale` block of `exec-approval.md`" in prelude
    assert "run the `PR code = do not run` block of `exec-approval.md`" in prelude
    assert "write the word `stale` as the sole line" not in gate


def _flag(world: Path, value: str = "true") -> None:
    """Record the ``--sandbox`` flag sentinel the way the argument parser leaves it."""
    (world / "tmp" / "resolve-sandbox-t1").write_text(value + "\n", encoding="utf-8", newline="\n")


def test_the_sandbox_flag_binds_the_sandbox_tier_and_asks_nothing(world: Path) -> None:
    """With ``--sandbox`` a scan the helper calls skippable records ``sandbox`` at the sandbox tier, review record or
    not."""
    _flag(world)
    run = _run(world, STUB_CHECK="sandbox")
    assert run.plan == "pr-code=recorded hooks=run"
    assert run.decision == "sandbox\n"
    (check,) = run.called("check")
    assert "check-sandbox" in check
    assert "--status" not in check
    (bind,) = [line for line in run.called("guard") if line.startswith("guard bind")]
    assert "--tier sandbox" in bind
    assert "unsandboxed" not in bind


def test_the_sandbox_flag_reports_tests_not_run_on_a_host_without_a_sandbox(world: Path) -> None:
    """A host that cannot start the sandbox never runs PR code under the flag: the decision is ``deny``, nothing is
    bound."""
    _flag(world)
    run = _run(world, STUB_CHECK="deny reason=no-sandbox")
    assert run.plan == "pr-code=recorded hooks=run"
    assert run.decision == "deny\n"
    assert not [line for line in run.called("guard") if line.startswith("guard bind")]


@pytest.mark.parametrize(
    "check",
    [
        pytest.param("ask reason=findings-need-decision", id="evidence-to-decide-on"),
        pytest.param("sandbox extra", id="check-adds-words"),
        pytest.param("auto", id="the-review-skip-word-is-not-a-sandbox-decision"),
        pytest.param("", id="check-prints-nothing"),
    ],
)
def test_the_sandbox_flag_asks_unless_the_check_says_exactly_sandbox(world: Path, check: str) -> None:
    """Only the exact word ``sandbox`` skips the question under the flag; everything else asks and binds nothing."""
    _flag(world)
    run = _run(world, STUB_CHECK=check)
    assert run.plan == "pr-code=ask hooks=run"
    assert run.decision is None
    assert not [line for line in run.called("guard") if line.startswith("guard bind")]


def test_a_flag_that_is_not_true_leaves_the_default_rules(world: Path) -> None:
    """Anything but the word ``true`` in the sentinel keeps the review-record rule, so a stray value never enforces."""
    _flag(world, "false")
    run = _run(world, STUB_CHECK="sandbox")
    assert run.plan == "pr-code=ask hooks=run"
    (check,) = run.called("check")
    assert "--status" in check


def _prior_approval(world: Path) -> None:
    """Leave the approval and the ``stale`` word a guard refusal would have left behind."""
    (world / "impl" / "code-approval.txt").write_text("{}\n", encoding="utf-8", newline="\n")
    (world / "impl" / "pr-code-decision.txt").write_text("stale\n", encoding="utf-8", newline="\n")


def test_a_clean_rescan_renews_a_stale_approval_without_a_question(world: Path) -> None:
    """The merged tree is rescanned; a clean result binds the sandbox tier at the current commit and records
    ``sandbox``."""
    _prior_approval(world)
    done = _stale_block(world, STUB_CHECK="auto")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (world / "impl" / "pr-code-decision.txt").read_text(encoding="utf-8") == "sandbox\n"
    log = (world / "stub.log").read_text(encoding="utf-8").splitlines()
    (check,) = [line for line in log if line.startswith("check")]
    assert "check-rebind" in check
    assert "code-approval.set-aside" in check
    (bind,) = [line for line in log if line.startswith("guard bind")]
    assert f"--head {_head(_checkout(world))} " in bind
    assert "--tier sandbox" in bind
    assert "no question" in done.stdout


def test_a_rescan_with_anything_to_decide_keeps_the_stale_word_and_binds_nothing(world: Path) -> None:
    """Any answer but ``auto`` leaves the decision ``stale``, so the question is asked as before."""
    _prior_approval(world)
    done = _stale_block(world, STUB_CHECK="ask reason=findings-present")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (world / "impl" / "pr-code-decision.txt").read_text(encoding="utf-8") == "stale\n"
    log = (world / "stub.log").read_text(encoding="utf-8").splitlines()
    assert not [line for line in log if line.startswith("guard bind")]


def test_a_failed_rescan_keeps_the_stale_word(world: Path) -> None:
    """Without a fresh scan nothing is judged clean, so the question is asked as before."""
    _prior_approval(world)
    done = _stale_block(world, STUB_SCAN_RC="1", STUB_CHECK="auto")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (world / "impl" / "pr-code-decision.txt").read_text(encoding="utf-8") == "stale\n"
    assert not [line for line in (world / "stub.log").read_text(encoding="utf-8").splitlines() if "check" in line[:6]]


def test_the_sandbox_flag_renews_through_the_sandbox_check(world: Path) -> None:
    """Under ``--sandbox`` the renewal asks the ``check-sandbox`` rule, not the prior-approval rule."""
    _flag(world)
    _prior_approval(world)
    done = _stale_block(world, STUB_CHECK="sandbox")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (world / "impl" / "pr-code-decision.txt").read_text(encoding="utf-8") == "sandbox\n"
    log = (world / "stub.log").read_text(encoding="utf-8").splitlines()
    (check,) = [line for line in log if line.startswith("check")]
    assert "check-sandbox" in check
    assert "--prior" not in check


def test_a_second_refusal_at_the_same_commit_rescans_nothing(world: Path) -> None:
    """Uncommitted work cannot be covered by a committed approval: the final refusal denies before any rescan."""
    impl = world / "impl"
    (impl / "pr-code-rebind-head.txt").write_text(_head(_checkout(world)) + "\n", encoding="utf-8", newline="\n")
    (impl / "code-approval.txt").write_text("approval\n", encoding="utf-8", newline="\n")
    done = _stale_block(world, STUB_CHECK="auto")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (impl / "pr-code-decision.txt").read_text(encoding="utf-8") == "deny\n"
    assert not (world / "stub.log").exists()
