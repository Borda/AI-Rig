"""Contract tests for resolve's execution approvals (``modes/exec-approval.md``).

The skip rule decides whether contributor code may run without a question, so the fenced blocks that implement it are
parsed line by line: loosening a condition, adding an ``unsandboxed`` decision to the automatic path, or recording a
decision before the approval is bound must fail a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[3] / "skills/resolve"
_REPORT_TEMPLATE = _SKILL / "templates/resolve-report.md"


def _lines(relative: str) -> list[str]:
    """Return the lines of one resolve skill file."""
    return (_SKILL / relative).read_text(encoding="utf-8").splitlines()


def _bash_blocks(relative: str) -> list[list[str]]:
    """Return every fenced ``bash`` block of a skill file as a list of its lines."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in _lines(relative):
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip() == "```":
            blocks.append(current)
            current = None
        else:
            current.append(line)
    return blocks


def _block_with(blocks: list[list[str]], needle: str) -> list[str]:
    """Return the single block containing ``needle``; fail when it is missing or ambiguous."""
    found = [block for block in blocks if any(needle in line for line in block)]
    assert len(found) == 1, f"expected exactly one block with {needle!r}, found {len(found)}"
    return found[0]


def _index(block: list[str], needle: str) -> int:
    """Return the index of the first line holding ``needle``."""
    return next(i for i, line in enumerate(block) if needle in line)


@pytest.fixture(scope="module")
def decide() -> list[str]:
    """Return the Decide block of ``exec-approval.md``."""
    return _block_with(_bash_blocks("modes/exec-approval.md"), "exec-plan.txt")


def _auto_bind(decide: list[str]) -> str:
    """Return the only Decide bind line: the automatic approval for a clean scan."""
    (bind,) = [
        line for line in decide if "untrusted_code_guard.py" in line and " record " in line and "--tier" not in line
    ]
    return bind


def test_auto_bind_stays_in_the_sandbox(decide: list[str]) -> None:
    """Resolve runs merged and edited bytes no scan read, so the automatic bind never lowers the tier or adds an
    ``unsandboxed`` decision."""
    bind = _auto_bind(decide)
    assert "--scope run" in bind
    assert not any(flag in bind for flag in ("--tier", "--decision", "unsandboxed"))


def test_the_clean_scan_check_has_no_sandbox_condition_and_no_saved_choice(decide: list[str]) -> None:
    """The skip needs two clean scans of the same head and nothing else: no host-sandbox flag, no saved policy."""
    text = "\n".join(decide)
    assert "check --status" in text
    assert "--no-sandbox-needed" not in text
    assert "pr_code_policy" not in text
    assert "_POLICY" not in text


def test_auto_requires_the_exact_check_answer_and_a_successful_bind(decide: list[str]) -> None:
    """``auto`` is written only inside the branch that compared the check output exactly and then bound successfully."""
    bind_line = _auto_bind(decide)
    assert '[ "$_CHECK" = auto ]' in bind_line
    assert bind_line.rstrip().endswith("; then")
    writes = [i for i, line in enumerate(decide) if "echo auto >" in line]
    assert len(writes) == 1
    assert writes[0] > decide.index(bind_line)
    assert "code_safety_status.py" in "\n".join(decide[: decide.index(bind_line)])


def test_decision_words_that_count_as_recorded(decide: list[str]) -> None:
    """Only the four decision words are recorded; anything else, including ``stale``, asks again."""
    line = next(line for line in decide if '"$_DEC" = auto' in line)
    for word in ("auto", "sandbox", "unsandboxed", "deny"):
        assert f'"$_DEC" = {word}' in line
    assert "stale" not in line


def test_decision_file_read_survives_an_unterminated_line_and_fails_closed(decide: list[str]) -> None:
    """The Write tool may omit the trailing newline, so the read keeps what it got; an empty or unreadable file
    denies."""
    line = next(line for line in decide if "IFS= read -r _DEC" in line)
    assert '|| [ -n "$_DEC" ] || _DEC=deny' in line
    assert '_DEC="" ||' not in line


def test_an_earlier_ask_is_kept_not_recomputed(decide: list[str]) -> None:
    """A question found necessary at Step 4 stays one at Step 3d: neither the hook nor the scan is re-decided."""
    text = "\n".join(decide)
    assert 'read -r _PREV < "$IMPL_DIR/exec-plan.txt"' in text
    hooks = next(line for line in decide if 'hooks=ask}"' in line)
    code = next(line for line in decide if 'pr-code=ask}"' in line)
    assert "hooks-inspection.json" in hooks
    assert "code-scan.txt" in code


def test_sidecar_is_read_only_beside_the_report(decide: list[str]) -> None:
    """The sidecar path is derived only when ``REPORT_FILE`` holds a directory part; otherwise nothing is read."""
    guard = _index(decide, "*/*)")
    fallback = _index(decide, 'echo "ask reason=no-review-status"')
    assert guard < fallback
    assert not any("${REPORT_FILE%/*}" in line for line in decide[:guard])


def test_hook_decision_compares_the_pr_head(decide: list[str]) -> None:
    """The hook decision passes the PR head, so a staged base merge does not count as the PR's own change."""
    line = next(line for line in decide if "resolve_commit_hooks.py" in line and "--decide" in line)
    assert '--head "$PR_HEAD_OID"' in line


def test_failed_helpers_ask_at_the_gate_instead_of_deferring(decide: list[str]) -> None:
    """A failed scan shows an unavailable note and asks; a failed hook inspection keeps hooks disabled."""
    text = "\n".join(decide)
    assert "scan unavailable" in text
    assert ': > "$IMPL_DIR/approved-hooks.txt"; _HOOKS=disabled' in text
    assert "_PRCODE=deferred" not in text.split('if [ "$_XREPO" = false ]')[1]


def _answer_block(word: str) -> list[str]:
    """Return the answer-recording block that writes the decision word ``word``."""
    found = [
        block
        for block in _bash_blocks("modes/exec-approval.md")
        if not any("exec-plan.txt" in line for line in block)
        and any(f'echo {word} > "$IMPL_DIR/pr-code-decision.txt"' in line for line in block)
        and not any("git rev-parse" in line for line in block)  # the stale block denies and renews too
    ]
    assert len(found) == 1, f"expected one {word} block, found {len(found)}"
    return found[0]


def test_sandbox_answer_binds_without_unsandboxed() -> None:
    """Answering ``sandbox`` binds the default tier; only the unsandboxed answer adds the decision."""
    bind = next(line for line in _answer_block("sandbox") if " record " in line)
    assert not any(flag in bind for flag in ("--tier", "--decision", "unsandboxed"))
    assert '|| { echo "! BLOCKED' in bind
    block = _answer_block("sandbox")
    assert _index(block, " record ") < _index(block, "echo sandbox >")


def test_unsandboxed_answer_records_the_explicit_decision() -> None:
    """Only the explicit ``Run without sandbox`` answer binds ``--tier env-only --decision unsandboxed``."""
    block = _answer_block("unsandboxed")
    bind = next(line for line in block if " record " in line)
    assert "--tier env-only --decision unsandboxed" in bind
    assert _index(block, " record ") < _index(block, "echo unsandboxed >")


def test_deny_answer_records_no_approval() -> None:
    """``Do not run PR code`` writes the decision word and binds nothing."""
    block = _answer_block("deny")
    assert not any("untrusted_code_guard.py" in line for line in block)


def test_recovery_path_records_every_answer_and_stale_forces_a_new_question() -> None:
    """The Step 8 recovery records its answers too, and a stale guard refusal clears the recorded decision."""
    prelude = "\n".join(_lines("modes/dispatch-prelude.md"))
    assert "write `sandbox` or `unsandboxed` as the sole line of `$IMPL_DIR/pr-code-decision.txt`" in prelude
    assert "run the `PR code = do not run` block of `exec-approval.md` for **Do not run PR code**" in prelude
    gate = "\n".join(_lines("modes/lint-qa-gate.md"))
    assert "run the `PR code = stale` block of `exec-approval.md`" in gate


def test_checkout_no_longer_asks_and_step_4_loads_the_decision() -> None:
    """Step 4 decides without asking; the questions wait for Step 3d, last."""
    checkout = "\n".join(_lines("modes/checkout.md"))
    assert "run the Decide block of `modes/exec-approval.md`" in checkout
    assert "It asks nothing here" in checkout
    assert "follow `modes/dispatch-prelude.md` §Repository hook approval" not in checkout
    skill = "\n".join(_lines("SKILL.md"))
    assert "modes/checkout.md, modes/exec-approval.md" in skill


def test_report_line_states_the_approval_never_that_code_ran() -> None:
    """The final report wording is an approval statement, so a refusal or a skipped selection cannot contradict it."""
    text = _REPORT_TEMPLATE.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if line.startswith("> Only for a fork PR."))
    assert "sandbox approved without asking" in line
    assert "ran in sandbox" not in line
    assert "never that code ran" in line


def test_prelude_hook_recovery_keeps_an_earlier_ask() -> None:
    """The Step 8 recovery reads the saved plan before deciding, so a merge that absorbed the edit cannot loosen an
    ask."""
    blocks = _bash_blocks("modes/dispatch-prelude.md")
    guard = _block_with(blocks, "hooks=ask-kept")
    text = "\n".join(guard)
    assert 'read -r _PREV < "$IMPL_DIR/exec-plan.txt"' in text
    assert '[ -s "$IMPL_DIR/hooks-inspection.json" ]' in text
    assert '"${_PREV#*hooks=ask}" != "$_PREV"' in text
    prose = "\n".join(_lines("modes/dispatch-prelude.md"))
    assert "`hooks=ask-kept`: skip steps 1 and 2 and continue at step 3" in prose


def test_prelude_pr_code_word_without_an_approval_file_asks() -> None:
    """A recorded word counts only beside ``code-approval.txt``; otherwise the recovery asks."""
    prelude = "\n".join(_lines("modes/dispatch-prelude.md"))
    assert "One of those words without `code-approval.txt` is not recorded: ask as below." in prelude


def test_decide_runs_wherever_step_4_checks_out_a_pr_and_the_prelude_reads_an_empty_word_as_deny() -> None:
    """Report mode with a PR number checks out at Step 4 too, so it decides there; an empty word means deny
    everywhere."""
    skill = "\n".join(_lines("SKILL.md"))
    assert "`report` with a PR number in its header" in skill
    prelude = "\n".join(_lines("modes/dispatch-prelude.md"))
    assert "a present but empty or unreadable file (the Decide block reads it as `deny` too)" in prelude
    approval = "\n".join(_lines("modes/exec-approval.md"))
    assert "`report` mode with a PR number Step 3d comes before the checkout" in approval


def test_every_approved_hooks_read_keeps_an_unterminated_digest() -> None:
    """The digest file may lack a trailing newline, so each reader keeps what it read instead of clearing it."""
    readers = [
        (path.name, line)
        for path in sorted((_SKILL / "modes").glob("*.md"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "read -r RESOLVE_APPROVED_HOOKS" in line
    ]
    assert len(readers) >= 7
    for name, line in readers:
        assert '[ -n \\"$RESOLVE_APPROVED_HOOKS\\" ]' in line or '[ -n "$RESOLVE_APPROVED_HOOKS" ]' in line, name


def test_report_template_discloses_an_automatic_skip_in_every_mode_that_can_record_one() -> None:
    """The ``PR code:`` line is omitted only where no decision exists, never by mode, since ``report`` mode with a PR
    can skip."""
    (line,) = [row for row in _REPORT_TEMPLATE.read_text(encoding="utf-8").splitlines() if "Omit the line for" in row]
    assert "and in `report` mode" not in line
    assert "report` run with no PR number" in line
    changelog = [row for row in (_SKILL.parents[1] / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()[:8]]
    assert not any("`report` mode and a resumed run keep the old ask" in row for row in changelog)


def test_report_row_says_a_clean_scan_approved_the_sandbox_without_asking() -> None:
    """The ``auto`` report wording names the sandbox and the head, since the question never appears."""
    line = next(
        row for row in _REPORT_TEMPLATE.read_text(encoding="utf-8").splitlines() if row.startswith("> Only for")
    )
    assert "`auto`: `sandbox approved without asking" in line
    assert "WITHOUT sandbox approved without asking" not in line
    assert "pr_code_policy" not in line
    assert "pr-code-remembered" not in line


def test_skill_and_readme_describe_the_clean_scan_run_without_a_saved_choice() -> None:
    """Every statement of the skip rule says the tests run in the sandbox, and none mentions a saved choice."""
    for name in ("SKILL.md", "modes/exec-approval.md", "modes/dispatch-prelude.md"):
        text = "\n".join(_lines(name))
        assert "pr_code_policy" not in text, name
        assert "pr-code-remembered" not in text, name
    skill = "\n".join(_lines("SKILL.md"))
    assert "and the tests run in the sandbox, when `oss:review` scanned the same head" in skill
    assert "without the sandbox" not in skill
    assert "the sandbox as the first option" in skill


def test_a_remembered_hook_approval_binds_the_digest_the_user_was_shown() -> None:
    """Both places that run ``--remember approved`` pass the displayed digest, since hooks can change before the
    answer."""
    for name in ("modes/exec-approval.md", "modes/dispatch-prelude.md"):
        text = "\n".join(_lines(name))
        assert "--remember approved --sha <the displayed sha256>" in text, name
        assert "--remember approved`" not in text.replace("--remember approved --sha", ""), name


def test_a_stale_refusal_rebinds_at_the_commit_it_was_about() -> None:
    """The stale block saves the current HEAD and the sandbox and unsandboxed answers bind it, so the re-asked approval
    is not stale again by construction."""
    blocks = _bash_blocks("modes/exec-approval.md")
    stale = _block_with(blocks, "echo stale >")
    assert _index(stale, "git rev-parse HEAD > ") < _index(stale, "echo stale >")
    for word in ("sandbox", "unsandboxed"):
        block = _answer_block(word)
        bind = next(line for line in block if " record " in line)
        assert '--head "$_BIND_HEAD"' in bind
        assert any("pr-code-rebind-head.txt" in line and "_BIND_HEAD=" in line for line in block[: block.index(bind)])
    prelude = "\n".join(_lines("modes/dispatch-prelude.md"))
    assert "pr-code-rebind-head.txt" in prelude


def test_every_statement_of_the_skip_keeps_the_sandbox_and_names_both_scans() -> None:
    """Checkout, README and the approval doc each say the clean-scan skip asks nothing but keeps the sandbox."""
    checkout = "\n".join(_lines("modes/checkout.md"))
    assert "resolve skips the question and still runs the tests in the sandbox" in checkout
    assert "this host has a usable sandbox" in checkout
    assert "either scan did not find clean" in checkout
    approval = "\n".join(_lines("modes/exec-approval.md"))
    assert "a clean scan skips the question; the runs stay in the sandbox" in approval
    assert "never `--decision unsandboxed`" in approval
    readme = "\n".join((_SKILL.parents[1] / "README.md").read_text(encoding="utf-8").splitlines())
    assert "resolve's runs stay in the sandbox" in readme
    for text in (checkout, approval, readme):
        assert "resolve runs the tests WITHOUT the sandbox" not in text
        assert "resolve run PR code without the sandbox" not in text


def test_the_stale_exception_to_unattended_run_two_is_stated_where_the_claim_is_made() -> None:
    """The unattended-run claim and the approval doc both name the one stale re-ask and its bound."""
    coordination = "\n".join(_lines("modes/run-coordination.md"))
    assert "a second refusal at the same commit is a reported gap, not another question" in coordination
    approval = "\n".join(_lines("modes/exec-approval.md"))
    assert "A second refusal at the same commit" in approval
    readme = "\n".join((_SKILL.parents[1] / "README.md").read_text(encoding="utf-8").splitlines())
    assert "a second refusal at the same commit is reported as tests not run" in readme


def test_the_prelude_check_fence_blocks_on_a_missing_impl_dir() -> None:
    """An empty ``IMPL_DIR`` stops the check fence instead of reading and writing at the filesystem root."""
    fence = _block_with(_bash_blocks("modes/dispatch-prelude.md"), "hooks=ask-kept")
    assert _index(fence, "IMPL_DIR sentinel missing") < _index(fence, "exec-plan.txt")


def test_the_sandbox_flag_binds_the_sandbox_tier_only_after_the_exact_check_word(decide: list[str]) -> None:
    """Under ``--sandbox`` the bind names ``--tier sandbox`` and sits inside the ``sandbox`` arm of the check, never
    beside an unsandboxed decision."""
    (bind,) = [line for line in decide if " record " in line and "--tier sandbox" in line]
    assert "unsandboxed" not in bind
    arm = _index(decide, "sandbox)")
    assert "check-sandbox" in "\n".join(decide[:arm])
    assert arm < decide.index(bind)
    assert 'case "$_CHECK" in' in "\n".join(decide[:arm])
