"""Guard oss:review Step 7's already-running detection and its guarded contract clear."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"
_BASH = shutil.which("bash")


def _bash_block_after(skill: str, heading: str) -> str:
    """Return the executable Bash block right after a unique review-skill heading."""
    start = skill.index("```bash", skill.index(heading)) + len("```bash\n")
    return skill[start : skill.index("```", start)]


@pytest.mark.skipif(_BASH is None, reason="Step 7 already-running check uses Bash")
@pytest.mark.parametrize(
    ("pr_tag", "resolve_sentinel", "expected"),
    [
        pytest.param("1542", None, False, id="no-resolve-sentinel"),
        pytest.param("1542", "1542", True, id="matching-pr"),
        pytest.param("1542", "999", False, id="different-pr"),
        pytest.param("", "1542", False, id="own-pr-unknown-never-suppresses"),
        pytest.param("1542", "report", False, id="resolve-in-report-mode-no-pr-to-match"),
    ],
)
def test_already_running_check_matches_resolve_sentinel_by_pr(
    tmp_path: Path, pr_tag: str, resolve_sentinel: str | None, expected: bool
) -> None:
    """The Step 7 gate must skip AskUserQuestion only when resolve is active for THIS PR.

    Reads resolve's own dedicated liveness sentinel (`oss-resolve-active-<CSID>`), never `.temp/state/skill-contract.md`
    — that file is review's own compaction contract, rewritten unconditionally at this skill's own Step 0/5b/8, so a
    check reading it would never actually observe a running resolve (the original, broken design). A missing sentinel, a
    mismatched PR, or an unknown own-PR must never suppress the gate.
    """
    skill = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
    block = _bash_block_after(skill, "**Already-running check**")

    session = "review-active-test"
    (tmp_path / f"oss-review-pr-tag-{session}").write_text(f"{pr_tag}\n", encoding="utf-8", newline="\n")
    if resolve_sentinel is not None:
        (tmp_path / f"oss-resolve-active-{session}").write_text(f"{resolve_sentinel}\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    if expected:
        assert f"already running in this session for PR {pr_tag}" in result.stdout
    else:
        assert result.stdout == ""


@pytest.mark.skipif(_BASH is None, reason="Step 5b's own contract write must not affect the check")
def test_review_own_contract_write_does_not_affect_already_running_check(tmp_path: Path) -> None:
    """The check must ignore `.temp/state/skill-contract.md` entirely.

    Regression for the incident where the check read that file, but review's own Step 5b overwrites it unconditionally
    before Step 7 runs — so in every real ordering the check saw review's own contract, never resolve's, and the gate
    fired regardless.
    """
    skill = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
    block = _bash_block_after(skill, "**Already-running check**")

    session = "review-clobber-test"
    (tmp_path / f"oss-review-pr-tag-{session}").write_text("1542\n", encoding="utf-8", newline="\n")
    (tmp_path / f"oss-resolve-active-{session}").write_text("1542\n", encoding="utf-8", newline="\n")
    contract_dir = tmp_path / ".temp/state"
    contract_dir.mkdir(parents=True)
    contract_dir.joinpath("skill-contract.md").write_text(
        "- skill: oss:review · phase: reply (after consolidation)\n- preserve: pr=1542\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "already running in this session for PR 1542" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="Step 7b's contract clear uses Bash")
@pytest.mark.parametrize(
    ("resolve_sentinel", "should_clear"),
    [
        pytest.param(None, True, id="resolve-inactive-clears-contract"),
        pytest.param("1542", False, id="resolve-active-preserves-contract"),
    ],
)
def test_7b_contract_clear_reflects_fresh_state(
    tmp_path: Path, resolve_sentinel: str | None, should_clear: bool
) -> None:
    """7b must re-check resolve's liveness fresh, not trust a stale pre-idle snapshot.

    Regression for the TOCTOU where the original design computed `_RESOLVE_ACTIVE` once before 7a and reused it at 7b —
    resolve can start or finish during 7a's own idle window (up to 11h on a real review), so a snapshot taken before the
    wait is stale by the time it matters.
    """
    skill = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
    block = _bash_block_after(skill, "### 7b — Confidence block")

    session = "review-clear-test"
    (tmp_path / f"oss-review-pr-tag-{session}").write_text("1542\n", encoding="utf-8", newline="\n")
    if resolve_sentinel is not None:
        (tmp_path / f"oss-resolve-active-{session}").write_text(f"{resolve_sentinel}\n", encoding="utf-8", newline="\n")
    contract_dir = tmp_path / ".temp/state"
    contract_dir.mkdir(parents=True)
    contract_path = contract_dir / "skill-contract.md"
    contract_path.write_text("- skill: oss:review · phase: x\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert contract_path.exists() is not should_clear


@pytest.mark.skipif(_BASH is None, reason="TOCTOU regression uses Bash")
def test_7b_does_not_trust_7a_snapshot_across_the_idle_window(tmp_path: Path) -> None:
    """Resolve becoming active during 7a's idle must still protect the contract at 7b.

    The already-running check ran once, before the idle gate, and found resolve inactive (gate fired normally). While
    the gate idled, resolve started for this same PR. 7b must detect that fresh state and refuse to clear the contract,
    even though 7a's own check — run before resolve existed — said inactive.
    """
    skill = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
    already_running_block = _bash_block_after(skill, "**Already-running check**")
    seven_b_block = _bash_block_after(skill, "### 7b — Confidence block")

    session = "review-toctou-test"
    (tmp_path / f"oss-review-pr-tag-{session}").write_text("1542\n", encoding="utf-8", newline="\n")
    contract_dir = tmp_path / ".temp/state"
    contract_dir.mkdir(parents=True)
    contract_path = contract_dir / "skill-contract.md"
    contract_path.write_text("- skill: oss:review · phase: x\n", encoding="utf-8", newline="\n")
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)}

    before = subprocess.run([_BASH, "-c", already_running_block], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert before.returncode == 0, before.stderr
    assert before.stdout == ""  # resolve not active yet — gate would fire normally

    # resolve starts for this same PR during the idle gate
    (tmp_path / f"oss-resolve-active-{session}").write_text("1542\n", encoding="utf-8", newline="\n")

    after = subprocess.run([_BASH, "-c", seven_b_block], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert after.returncode == 0, after.stderr
    assert contract_path.exists()
