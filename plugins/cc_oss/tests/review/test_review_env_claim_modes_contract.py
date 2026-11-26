"""Contract tests for the review modes that provision the reviewer environment and reproduce author claims.

The modes are executed by the review's parent from fixed Bash blocks. These tests pin the parts a later edit could
silently weaken: the provisioner writes one JSON document and never installs from the block itself; the claim blocks
authorize only through the existing PR-code gate's no-question paths, empty an approval before recording it, never ask a
new question, and hand the run the recorded clause.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest

_MODES = Path(__file__).resolve().parents[2] / "skills/review/modes"
#: The guard subcommand that records an approval (``bind`` before its rename; the guard keeps that alias).
_RECORD = 'untrusted_code_guard.py" record --repo '
_BASH = shutil.which("bash")
_SHELL = pytest.mark.skipif(_BASH is None or shutil.which("jq") is None, reason="Claim steps use Bash and jq")


@cache
def _blocks(name: str) -> tuple[tuple[str, ...], ...]:
    """Return each fenced Bash block of a mode file as a tuple of lines."""
    lines = (_MODES / name).read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[str, ...]] = []
    start = None
    for index, line in enumerate(lines):
        if line == "```bash":
            start = index + 1
        elif line == "```" and start is not None:
            blocks.append(tuple(lines[start:index]))
            start = None
    return tuple(blocks)


@cache
def _text(name: str) -> str:
    """Read one mode file."""
    return (_MODES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "consumer"),
    [
        pytest.param("provision-env.md", "plugins/cc_oss/skills/review/SKILL.md", id="provision-env"),
        pytest.param("claim-repro.md", "plugins/cc_oss/skills/review/SKILL.md", id="claim-repro"),
    ],
)
def test_mode_declares_its_consumers(name: str, consumer: str) -> None:
    """Each new mode file opens with the consumer header the shared-file rule requires.

    An unreferenced mode file is deleted by the orphan check, taking the procedure with it.
    """
    first = _text(name).splitlines()[0]
    assert (first.startswith(f"<!-- file: {name} — consumers: "), consumer in first) == (True, True)


def test_provision_block_writes_the_document_and_installs_nothing_itself() -> None:
    """The block calls the provisioner once with ``--out``; no installer command appears in the block.

    Installs go through the helper's trust classification only; a hand-written ``pip install`` would bypass it.
    """
    block = "\n".join(_blocks("provision-env.md")[0])
    assert 'review_env_provision.py" --repo . --base "$BASE_SHA" --head "$PR_HEAD_SHA"' in block
    assert '--out "$RUN_DIR/review-env.json"' in block
    assert re.search(r"\b(?:pip|uv)\s+(?:pip\s+)?install\b", block) is None


def test_claim_modes_never_ask_a_new_question() -> None:
    """Claim reproduction adds no approval question: it uses only approvals the gate grants without asking.

    An operation that ran without a stop must keep running without one.
    """
    assert "AskUserQuestion" not in _text("claim-repro.md")


class TestAuthorizeBlock:
    """The authorize block records an approval only for what the gate's own check printed, one side at a time."""

    @pytest.fixture
    def block(self) -> tuple[str, ...]:
        """Return the authorize block's lines."""
        return next(b for b in _blocks("claim-repro.md") if any(_RECORD in ln for ln in b))

    def test_approval_is_emptied_before_recording(self, block: tuple[str, ...]) -> None:
        """Each side's approval file is truncated first, so a failed record never leaves an earlier approval usable.

        The run treats an empty approval file as no approval.
        """
        empty = next(i for i, ln in enumerate(block) if ln.strip().startswith(': > "$RUN_DIR/claim-approval-'))
        bind = next(i for i, ln in enumerate(block) if _RECORD in ln)
        assert empty < bind

    def test_record_follows_the_gate_check(self, block: tuple[str, ...]) -> None:
        """The approval is recorded only after the check printed exactly the expected word (``auto`` or ``sandbox``).

        Both tiers bind the exact benchmark revision, preserving the review's choice of isolation.
        """
        check = next(i for i, ln in enumerate(block) if '[ "$_CHECK" = "$_WANT" ] ||' in ln)
        bind = next(i for i, ln in enumerate(block) if _RECORD in ln)
        tiers = next(ln for ln in block if "_MODE=check-sandbox" in ln)
        assert check < bind
        assert (
            "_TIER=(--tier sandbox --decision exact-head)" in tiers,
            "_TIER=(--tier env-only --decision unsandboxed --decision exact-head)" in tiers,
        ) == (True, True)

    def test_only_a_recorded_approval_clause_starts_authorization(self, block: tuple[str, ...]) -> None:
        """A clause that records no approval (the user declined, or the scan was not clean) stops the block.

        Same-repo PRs need no approval, so they stop before the clause check too.
        """
        clause = next(i for i, ln in enumerate(block) if ln.startswith("grep ") and "code-clause.txt" in ln)
        same_repo = next(i for i, ln in enumerate(block) if ".isCrossRepository == false" in ln)
        bind = next(i for i, ln in enumerate(block) if _RECORD in ln)
        assert same_repo < clause < bind


def test_run_block_hands_over_the_recorded_clause() -> None:
    """The run reads the clause, the snapshot and both approval files; it decides nothing on its own.

    Without the clause the helper runs nothing, so dropping the flag would silently turn every claim ``not-run``.
    """
    block = "\n".join(next(b for b in _blocks("claim-repro.md") if any('review_claim_repro.py" run' in ln for ln in b)))
    flags = (
        '--clause-file "$RUN_DIR/code-clause.txt"',
        '--pr-meta "$SNAP_DIR/pr-meta.json"',
        '--head-approval "$RUN_DIR/claim-approval-head.txt"',
        '--base-approval "$RUN_DIR/claim-approval-base.txt"',
    )
    assert [flag in block for flag in flags] == [True] * len(flags)  # fmt: skip


@pytest.fixture
def claim_step_env(tmp_path: Path) -> dict[str, str]:
    """Prepare private sentinels and a snapshot for executing the shipped plan and run fences."""
    session = "claim-step"
    for kind in ("run", "snap"):
        (tmp_path / f"oss-review-{kind}-dir-{session}").write_text(
            tmp_path.as_posix() + "\n", encoding="utf-8", newline="\n"
        )
    (tmp_path / "pr-meta.json").write_text('{"baseRefOid":"base","headRefOid":"head"}', encoding="utf-8")
    (tmp_path / "claims.json").write_text('{"claims":[{"id":"c1"},{"id":"c2"}]}', encoding="utf-8")
    return os.environ | {
        "CLAUDE_CODE_SESSION_ID": session,
        "CLAUDE_PLUGIN_ROOT": tmp_path.as_posix(),
        "TMPDIR": tmp_path.as_posix(),
    }


def _run_claim_step(step: str, env: dict[str, str], helper: str) -> subprocess.CompletedProcess[str]:
    """Run the actual Bash and jq fence while supplying a controlled Python helper response."""
    block = next(b for b in _blocks("claim-repro.md") if any(f'review_claim_repro.py" {step} ' in ln for ln in b))
    stub = 'python() {\n  case "$1" in */resolve_plugin_root.py) printf "%s\\n" "$CLAUDE_PLUGIN_ROOT"; return;; esac\n'
    script = "\n".join((stub, helper, "}", *block))
    return subprocess.run([_BASH, "-c", script], cwd=env["TMPDIR"], env=env, capture_output=True, text=True, timeout=15)


@_SHELL
@pytest.mark.integration
@pytest.mark.parametrize(
    ("listed_ids", "expected"),
    [
        pytest.param(b"c1", ["--claim", "c1"], id="sole-unterminated-id"),
        pytest.param(b"c1\nc2", ["--claim", "c1", "--claim", "c2"], id="final-unterminated-id"),
        pytest.param(b"c1\nc2\n", ["--claim", "c1", "--claim", "c2"], id="terminated-ids"),
    ],
)
def test_plan_passes_every_claim_id_without_requiring_a_trailing_newline(
    tmp_path: Path, claim_step_env: dict[str, str], listed_ids: bytes, expected: list[str]
) -> None:
    """The plan passes a sole or final unterminated coverage ID to the helper."""
    (tmp_path / "claim-bench-claims.txt").write_bytes(listed_ids)
    plan = '{"status":"ready","claims":[],"candidates":[]}'
    done = _run_claim_step(
        "plan",
        claim_step_env,
        f"""printf "%s\\n" "$@" > "$TMPDIR/plan-argv.txt"; printf '%s' '{plan}' > "${{!#}}" """,
    )
    assert done.returncode == 0, done.stderr
    argv = (tmp_path / "plan-argv.txt").read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("--work-dir") + 2 : -2] == expected


@_SHELL
@pytest.mark.integration
@pytest.mark.parametrize(
    ("step", "artifact"),
    [
        pytest.param("plan", "claim-plan.json", id="plan"),
        pytest.param("run", "claims-result.json", id="run"),
    ],
)
def test_failed_claim_step_discards_earlier_output_and_propagates_failure(
    tmp_path: Path, claim_step_env: dict[str, str], step: str, artifact: str
) -> None:
    """A failed helper sees no previous output and cannot print a previous invocation's result."""
    (tmp_path / "claim-plan.json").write_text(
        '{"status":"ready","reason":"stale plan","claims":[],"candidates":[]}', encoding="utf-8"
    )
    (tmp_path / "claims-result.json").write_text(
        '{"claims":[{"id":"c1","verdict":"stale verdict","reason":"old result"}]}', encoding="utf-8"
    )
    done = _run_claim_step(step, claim_step_env, '[ ! -e "${!#}" ] || return 24; return 23')
    assert (done.returncode, done.stdout, (tmp_path / artifact).exists()) == (23, "", False)
    assert not (tmp_path / "claims-result.json").exists()
