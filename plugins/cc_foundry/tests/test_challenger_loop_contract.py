"""Keep foundry:challenger usable as the adversarial loop's reviewer: a round must fold into the loop table."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[2]
_CHALLENGER = _PLUGINS / "cc_foundry" / "agents" / "challenger.md"


def _find_bash() -> str | None:
    """Find a working Bash, preferring Git Bash to the Windows WSL launcher."""
    roots = (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles(x86)"))
    candidates = [str(Path(root) / "Git" / sub / "bash.exe") for root in roots if root for sub in ("bin", "usr/bin")]
    if found := shutil.which("bash"):
        candidates.append(found)
    for candidate in candidates:
        try:
            probe = subprocess.run([candidate, "-c", "printf ok"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout == "ok":
            return candidate
    return None


_BASH = _find_bash()


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param("never `run_in_background`", id="foreground-only"),
        pytest.param("Final reply = deliverable", id="inline-delivery"),
        pytest.param("Never send the report or envelope through SendMessage", id="no-sendmessage-envelope"),
        pytest.param("never author candidate bypass strings", id="generator-not-author"),
        pytest.param('"status": "blocked"', id="blocked-envelope"),
        pytest.param("Never report PASS for a search that did not run", id="no-false-pass"),
        pytest.param("the open-ended search runs in Codex, never in your own reply", id="codex-runs-filter-search"),
        pytest.param("A filter review needs Codex: ignore `--no-codex` for it", id="filter-review-keeps-codex"),
        pytest.param("tagged `source: codex` or `source: generator`", id="codex-candidates-counted"),
        pytest.param('"reason": "codex-unavailable"', id="codex-unavailable-blocks"),
        pytest.param("a skipped or failed cross-check is stated, never omitted", id="codex-section-always"),
        pytest.param("tools: Read, Write, Grep, Glob, Bash, Skill", id="skill-tool-for-bridge"),
    ],
)
def test_challenger_states_delivery_rule(clause: str) -> None:
    """Each rule that kept a challenger round from reaching the loop table stays in the agent definition.

    A challenger round failed once per missing rule: a background test run nothing resumed, a refused report-file write,
    an envelope sent as a SendMessage object, and a classifier stop while authoring bypass strings. Later rounds fed a
    generator file were still stopped once the agent searched on its own, and none reached Codex: a definition without
    the ``Skill`` tool cannot call ``bridge:review``, and the skip went unreported.
    """
    assert clause in _CHALLENGER.read_text(encoding="utf-8")


@pytest.mark.parametrize("tier", ["security", "critical", "high", "medium", "low", "nit"])
def test_challenger_maps_findings_to_loop_tier(tier: str) -> None:
    """Every adversarial-loop tier has a row in the challenger's mapping, so its counts fold into the progress table.

    The challenger's own Blocker/Concern/Nitpick scale has no weight in the loop score; without the mapping a completed
    round still cannot be scored.
    """
    assert f"| `{tier}` |" in _CHALLENGER.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "relative",
    [
        "cc_foundry/rules/_full/quality-gates.md",
        "cc_develop/rules/quality-gates.md",
        "cc_develop/rules/quality-gates-delta.md",
        "cc_oss/rules/quality-gates.md",
        "cc_oss/rules/quality-gates-delta.md",
        "cc_research/rules/quality-gates.md",
        "cc_research/rules/quality-gates-delta.md",
    ],
)
def test_dispatch_site_records_challenger_failure(relative: str) -> None:
    """Every rule that dispatches the challenger in a loop treats a round without a deliverable as an agent failure.

    A silent ``not-run`` row hides why the primary reviewer produced nothing; the rule asks for the cause and one
    corrected re-dispatch, and keeps Codex enabled so the challenger's own cross-check runs the bypass search. A reply
    cut off before its closing block is listed as a cause too, or it reads as a complete round with fewer findings.
    """
    text = (_PLUGINS / relative).read_text(encoding="utf-8")
    assert "agent failure" in text
    assert "reply truncated before its required closing block" in text
    assert "probe generator" in text
    assert "no `--no-codex`" in text


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param("is an agent failure, never a silent `not-run`", id="failure-not-silent"),
        pytest.param("never append it to `loop-rounds.jsonl`", id="failure-never-a-round"),
        pytest.param(
            "Re-dispatch that reviewer once with a brief that removes the cause;", id="unconditional-redispatch"
        ),
        pytest.param("not a retry of the same one", id="redispatch-not-retry"),
        pytest.param("still needs any approval its route requires", id="redispatch-keeps-approval"),
        pytest.param("for a refused write, require the findings inline in the final reply", id="refused-write-inline"),
        pytest.param("never spend the re-dispatch on the same write under another name", id="no-rename-retry"),
        pytest.param("treat the route as failed without a re-dispatch", id="no-attempt-left-fails-route"),
        pytest.param("switch to another permitted independent route when one exists", id="second-failure-switches"),
        pytest.param("stop as `independence-unavailable` only when none remains", id="stop-only-without-route"),
        pytest.param("a reply truncated in transit", id="truncated-reply-is-failure"),
        pytest.param(
            "A truncated reply never counts as a complete round with fewer findings", id="truncated-not-short-round"
        ),
        pytest.param("keep asking while each reply adds new content", id="truncated-tail-recovery"),
        pytest.param("A reply that adds nothing new", id="truncated-tail-stalls"),
    ],
)
def test_loop_rule_records_reviewer_round_failure(clause: str) -> None:
    """The shipped loop rule treats any reviewer round without a deliverable as a recorded agent failure.

    The quality-gates files cover the challenger; the loop rule itself must say the same for every reviewer, or a caller
    reading only ``_full/adversarial-loop.md`` records the round as ``not-run`` and loses its cause.
    """
    text = (_PLUGINS / "cc_foundry/rules/_full/adversarial-loop.md").read_text(encoding="utf-8")
    assert clause in text


def test_loop_rule_redispatch_is_not_gated_on_retry_contract() -> None:
    """The corrected re-dispatch is an obligation, not an option a workflow's retry contract can veto.

    Codex Rig's specialist retry policy allows only timeout, transport and rate-limit retries; a qualifier tying the re-
    dispatch to that contract silently removed it there while every rule and changelog promised it.
    """
    text = (_PLUGINS / "cc_foundry/rules/_full/adversarial-loop.md").read_text(encoding="utf-8")
    assert "Where the owning workflow's retry contract permits that class, re-dispatch" not in text


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param("Name the signature after the violated invariant", id="invariant-signature"),
        pytest.param("a new route to a known invariant is a repeat, never a new finding", id="route-is-repeat"),
        pytest.param("An `unmodeled` cell is a finding, never a coverage note", id="unmodeled-is-finding"),
        pytest.param("Every review round re-fills the whole table", id="full-table-every-round"),
        pytest.param("a search that finds no bypass proves only its own inputs", id="probe-not-coverage"),
        pytest.param("changing the deciding reader's default to refuse", id="fix-reader-default"),
        pytest.param("a check that refuses only inputs found in a hand-written set", id="hand-set-is-finding"),
        pytest.param(
            "so reuse the same invariant wording for every route of one invariant", id="recurrence-by-invariant"
        ),
        pytest.param("a later bypass inside a cell an earlier round marked `refused`", id="refused-cell-reopens"),
        pytest.param("Write the header before recording the first round, on every runtime", id="ledger-from-round-one"),
    ],
)
def test_loop_rule_closes_route_by_route_drift(clause: str) -> None:
    """The loop rule keys findings to invariants, demands grammar coverage for filters and a ledger on Claude too.

    A danger-gate loop ran eight rounds with new security findings every round: each was named after its route, so
    the repeat root-cause and third-occurrence rules never fired; each review searched by probe, so gaps surfaced one
    per round; each fix closed the cited route; and no Claude-side checker existed, so the run kept no ledger.
    """
    text = (_PLUGINS / "cc_foundry/rules/_full/adversarial-loop.md").read_text(encoding="utf-8")
    assert clause in text


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param("Coverage table: from the filtered language's authoritative grammar", id="coverage-table-step"),
        pytest.param("Every `unmodeled` cell is a finding", id="unmodeled-is-finding"),
        pytest.param("an incomplete table forbids a clean verdict", id="no-clean-without-table"),
        pytest.param("signature names the invariant it violates", id="invariant-signature"),
        pytest.param("`maps_to: <signature>`", id="maps-to-known"),
    ],
)
def test_challenger_filter_review_covers_grammar(clause: str) -> None:
    """A challenger security-filter review fills a grammar coverage table and names findings by invariant.

    Probe search alone found a few bypasses per round and named each after its route, hiding that every one broke an
    invariant an earlier round had already reported.
    """
    assert clause in _CHALLENGER.read_text(encoding="utf-8")


@pytest.mark.parametrize("plugin", ["cc_foundry", "cc_oss", "cc_develop", "cc_research"])
def test_claude_loop_checker_runs_standalone(plugin: str, tmp_path: Path) -> None:
    """Each Claude plugin's copy of the loop checker validates a ledger without the Codex plugin installed.

    The rule tells Claude loops to run a byte-identical copy; a copy that imported a Codex-only module would fail where
    it is the only checker available.
    """
    header = {
        "schema_version": 2,
        "implementation_author": "lead",
        "current_snapshot": {"revision": "HEAD", "diff_digest": "0" * 64},
    }
    ledger = tmp_path / "loop-ledger.json"
    ledger.write_text(json.dumps(header), encoding="utf-8")
    script = _PLUGINS / plugin / "bin" / "adversarial_loop.py"
    result = subprocess.run([sys.executable, str(script), "--ledger", str(ledger)], capture_output=True, text=True)
    assert json.loads(result.stdout)["status"] != "invalid", result.stdout + result.stderr


@pytest.mark.skipif(_BASH is None, reason="working bash not available")
def test_loop_rule_claude_command_resolves_without_plugin_root(tmp_path: Path) -> None:
    """The rule's Claude ledger command finds a checker when no plugin-root variable is set.

    A rule file gets no plugin-root substitution and the Bash tool exports none, so a command built on that variable
    named ``/bin/adversarial_loop.py`` and exited 2, leaving the loop ledger-less. The resolver line is run as the rule
    prints it, from the source tree with an empty home, so only its fallback can supply the checker.
    """
    text = (_PLUGINS / "cc_foundry/rules/_full/adversarial-loop.md").read_text(encoding="utf-8")
    resolver = re.search(r'`(_AL="\$\(ls -td [^`]+)`', text).group(1)
    header = {
        "schema_version": 2,
        "implementation_author": "lead",
        "current_snapshot": {"revision": "HEAD", "diff_digest": "0" * 64},
    }
    ledger = tmp_path / "loop-ledger.json"
    ledger.write_text(json.dumps(header), encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_PLUGIN_ROOT"} | {"HOME": str(tmp_path)}
    script = f'{resolver}; python "$_AL" --ledger "{ledger.as_posix()}"'
    result = subprocess.run([_BASH, "-c", script], cwd=_PLUGINS.parent, env=env, capture_output=True, text=True)
    assert json.loads(result.stdout)["status"] != "invalid", result.stdout + result.stderr


def _install_checker(checker: Path, mtime: int) -> None:
    """Place an installed checker copy in a fake plugin cache with a fixed modification time."""
    checker.parent.mkdir(parents=True)
    checker.write_text("", encoding="utf-8")
    os.utime(checker, (mtime, mtime))


@pytest.mark.skipif(_BASH is None, reason="working bash not available")
def test_loop_rule_claude_command_skips_orphaned_cache_copy(tmp_path: Path) -> None:
    """The rule's Claude resolver takes the newest installed checker whose version directory is not orphaned.

    The rule ships unchanged in four plugins, so it resolves any plugin's byte-identical copy by modification time. A
    version Claude Code has marked ``.orphaned_at`` is no longer installed, and a newest-first pick that ignored the
    marker ran it whenever it was touched last. The resolver line is run as the rule prints it against a fake cache.
    """
    text = (_PLUGINS / "cc_foundry/rules/_full/adversarial-loop.md").read_text(encoding="utf-8")
    resolver = re.search(r'`(_AL="\$\(ls -td [^`]+)`', text).group(1)
    cache = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig"
    _install_checker(cache / "oss" / "0.53.0" / "bin" / "adversarial_loop.py", 1_000_000_000)
    _install_checker(cache / "foundry" / "0.71.0" / "bin" / "adversarial_loop.py", 2_000_000_000)
    (cache / "foundry" / "0.71.0" / ".orphaned_at").write_text("1", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_PLUGIN_ROOT"} | {
        "HOME": tmp_path.as_posix()
    }

    result = subprocess.run(
        [_BASH, "-c", f'{resolver}; printf "%s" "$_AL"'], cwd=tmp_path, env=env, capture_output=True, text=True
    )

    assert result.stdout.endswith("/oss/0.53.0/bin/adversarial_loop.py"), result.stdout + result.stderr


def test_eager_foundry_rule_points_at_challenger_rounds() -> None:
    """The always-loaded foundry rule sends every challenger dispatch to the full Challenger Rounds section.

    The procedure lives in ``_full/quality-gates.md`` to keep the eager rule set small; without this pointer a caller
    would brief the challenger before ever reading it.
    """
    text = (_PLUGINS / "cc_foundry/rules/quality-gates.md").read_text(encoding="utf-8")
    assert "`_full/quality-gates.md` §Challenger Rounds" in text
