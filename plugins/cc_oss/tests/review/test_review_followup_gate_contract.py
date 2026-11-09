"""Guard oss:review Step 7's already-running detection and its guarded contract clear.

Both recipes are extracted from the shipped ``SKILL.md`` and run in a real Bash against sentinels under ``tmp_path``, so
a recipe edit that breaks the resolve-liveness contract fails here.
"""

import os
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest
from _text_io import bash_block_after, write_lf

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="The Step 7 recipes run in Bash")
_RUNNING = "already running in this session for PR 1542"


@cache
def _bash_block_after(heading: str) -> str:
    """Return the executable Bash block right after a unique review-skill heading."""
    return bash_block_after((_REVIEW / "modes/post-agent.md").read_text(encoding="utf-8"), heading)


def _env(tmp_path: Path, session: str) -> dict[str, str]:
    """Build the child environment: sentinels live in ``tmp_path`` under the given session token."""
    return os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)}


def _seed(tmp_path: Path, session: str, pr_tag: str, resolve_sentinel: str | None) -> None:
    """Persist the review PR tag and, when resolve is active, its liveness sentinel."""
    write_lf(tmp_path / f"oss-review-pr-tag-{session}", f"{pr_tag}\n")
    if resolve_sentinel is not None:
        write_lf(tmp_path / f"oss-resolve-active-{session}", f"{resolve_sentinel}\n")


def _seed_contract(tmp_path: Path, text: str) -> Path:
    """Create review's own compaction contract file under ``.temp/state`` and return its path."""
    contract = tmp_path / ".temp/state/skill-contract.md"
    contract.parent.mkdir(parents=True)
    write_lf(contract, text)
    return contract


def _run(block: str, tmp_path: Path, session: str) -> subprocess.CompletedProcess[str]:
    """Run one skill recipe in a fresh Bash with the session's sentinels and require a clean exit."""
    result = subprocess.run(
        [_BASH, "-c", block], cwd=tmp_path, env=_env(tmp_path, session), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result


@_skip_no_bash
@pytest.mark.parametrize(
    ("pr_tag", "resolve_sentinel", "shown"),
    [
        pytest.param("1542", None, False, id="no-resolve-sentinel"),
        pytest.param("1542", "1542", True, id="matching-pr"),
        pytest.param("1542", "999", False, id="different-pr"),
        pytest.param("", "1542", False, id="own-pr-unknown-never-suppresses"),
        pytest.param("1542", "report", False, id="resolve-in-report-mode-no-pr-to-match"),
    ],
)
def test_already_running_check_matches_resolve_sentinel_by_pr(
    tmp_path: Path, pr_tag: str, resolve_sentinel: str | None, shown: bool
) -> None:
    """The Step 7 gate must skip AskUserQuestion only when resolve is active for THIS PR.

    Reads resolve's own dedicated liveness sentinel (`oss-resolve-active-<CSID>`), never `.temp/state/skill-contract.md`
    — that file is review's own compaction contract, rewritten unconditionally at this skill's own Step 0/5b/8, so a
    check reading it would never actually observe a running resolve (the original, broken design). A missing sentinel, a
    mismatched PR, or an unknown own-PR must never suppress the gate.
    """
    _seed(tmp_path, "review-active-test", pr_tag, resolve_sentinel)

    result = _run(_bash_block_after("**Already-running check**"), tmp_path, "review-active-test")

    assert (f"already running in this session for PR {pr_tag}" in result.stdout, result.stdout == "") == (
        shown,
        not shown,
    )


@_skip_no_bash
def test_own_contract_write_does_not_affect_already_running_check(tmp_path: Path) -> None:
    """The check must ignore `.temp/state/skill-contract.md` entirely.

    Regression for the incident where the check read that file, but review's own Step 5b overwrites it unconditionally
    before Step 7 runs — so in every real ordering the check saw review's own contract, never resolve's, and the gate
    fired regardless.
    """
    _seed(tmp_path, "review-clobber-test", "1542", "1542")
    _seed_contract(tmp_path, "- skill: oss:review · phase: reply (after consolidation)\n- preserve: pr=1542\n")

    result = _run(_bash_block_after("**Already-running check**"), tmp_path, "review-clobber-test")

    assert _RUNNING in result.stdout


@_skip_no_bash
@pytest.mark.parametrize(
    ("resolve_sentinel", "cleared"),
    [
        pytest.param(None, True, id="resolve-inactive-clears-contract"),
        pytest.param("1542", False, id="resolve-active-preserves-contract"),
    ],
)
def test_7b_contract_clear_reflects_fresh_state(tmp_path: Path, resolve_sentinel: str | None, cleared: bool) -> None:
    """7b must re-check resolve's liveness fresh, not trust a stale pre-idle snapshot.

    Regression for the TOCTOU where the original design computed `_RESOLVE_ACTIVE` once before 7a and reused it at 7b —
    resolve can start or finish during 7a's own idle window (up to 11h on a real review), so a snapshot taken before the
    wait is stale by the time it matters.
    """
    _seed(tmp_path, "review-clear-test", "1542", resolve_sentinel)
    contract = _seed_contract(tmp_path, "- skill: oss:review · phase: x\n")

    _run(_bash_block_after("### 7b — Confidence block"), tmp_path, "review-clear-test")

    assert contract.exists() is not cleared


@_skip_no_bash
def test_7b_does_not_trust_7a_snapshot_across_the_idle_window(tmp_path: Path) -> None:
    """Resolve becoming active during 7a's idle must still protect the contract at 7b.

    The already-running check ran once, before the idle gate, and found resolve inactive (gate fired normally). While
    the gate idled, resolve started for this same PR. 7b must detect that fresh state and refuse to clear the contract,
    even though 7a's own check — run before resolve existed — said inactive.
    """
    session = "review-toctou-test"
    _seed(tmp_path, session, "1542", None)
    contract = _seed_contract(tmp_path, "- skill: oss:review · phase: x\n")
    before = _run(_bash_block_after("**Already-running check**"), tmp_path, session)
    write_lf(tmp_path / f"oss-resolve-active-{session}", "1542\n")  # resolve starts during the idle gate

    _run(_bash_block_after("### 7b — Confidence block"), tmp_path, session)

    assert (before.stdout, contract.exists()) == ("", True)
