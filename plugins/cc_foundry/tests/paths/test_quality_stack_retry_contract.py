"""Contract tests over ``quality-stack.md``'s flaky-retry and xdist-probe text.

Static assertions over the markdown source, not a bash-execution harness — this skill's bash blocks run only inside a
live Claude Code session, so pinning the specific patterns is the available regression guard, the way
``test_check_codemap_guard.py`` pins its own bash-preamble invariants. A future edit that silently reintroduces the old
blind double-retry, or drops the degenerate-empty-set guard, fails collection here instead of shipping.
"""

from __future__ import annotations

import re
from pathlib import Path

_FOUNDRY = Path(__file__).resolve().parent.parent.parent
_QUALITY_STACK = _FOUNDRY / "skills" / "_shared" / "quality-stack.md"


def _text() -> str:
    """Read the canonical quality-stack.md content once per call."""
    return _QUALITY_STACK.read_text(encoding="utf-8")


def _bash_fences(text: str) -> list[str]:
    """Return every fenced ```bash block's body, fence-boundary matched (never a naive greedy span).

    Anchors the closing marker to the SAME leading indent as the opening one — the cc_develop sibling checker (round-5
    M9 file-scope extension) found an unanchored version silently merges two fences when an indented open marker's own
    indented close is skipped past in favor of a later unindented one. quality-stack.md has no indented fences today,
    but the three call sites in this file all used the unsafe pattern directly — fixed at the source.
    """
    return [m.group(2) for m in re.finditer(r"^([ \t]*)```bash\n(.*?)\n\1```", text, re.MULTILINE | re.DOTALL)]


class TestFlakyRetryRedesign:
    """Pins the redesigned retry contract — see plan blockers 2, 3, 7."""

    def test_old_blind_double_retry_loop_removed(self) -> None:
        """The old ``for _i in 2 3`` blind whole-directory retry loop is gone.

        That loop re-ran the full test directory twice on any failure with no node-id narrowing — the largest confirmed
        cost source in the original investigation. Its return here would silently undo the redesign.
        """
        assert "for _i in 2 3" not in _text()

    def test_failing_node_ids_captured_from_full_untruncated_log(self) -> None:
        """Failing node-ids (FAILED and ERROR) come from a full log file, not a tail-capped stream."""
        text = _text()
        assert "FAILED_IDS" in text
        assert "grep -E '^(FAILED|ERROR) '" in text

    def test_node_id_extraction_anchors_on_id_structure_not_dash_search(self) -> None:
        """Node-id extraction matches pytest's own node-id grammar, not a `` - `` text search.

        ``awk '{print $2}'`` truncated at the first whitespace. A first fix (greedy ``(.*) - .*$``, rightmost split)
        over-captured whenever the *reason* itself contained `` - `` (e.g. ``assert 1 == 2 - extra``) — verified: it
        swallowed the reason's own dash into the id. The id is structurally a whitespace-free prefix plus at most one
        bracket group (``path::name[params]`` — only ``params`` may contain spaces/dashes); anchoring there is
        correct regardless of what the free-text reason contains, including a reason that itself has brackets.
        ``[^[:space:]]`` not ``\\S`` — BSD sed (macOS) has no ``\\S``.
        """
        text = _text()
        assert "| awk '{print $2}'" not in text
        assert "(.*) - .*$/\\2/" not in text  # the rejected greedy-dash-search form
        assert "sed -E 's/^(FAILED|ERROR) ([^[:space:]]+(\\[[^]]*\\])?).*$/\\2/'" in text

    def test_error_lines_retried_alongside_failed(self) -> None:
        """Fixture/collection ``ERROR`` lines feed the same retry path as ``FAILED`` lines.

        ``-rf`` alone reports only ``FAILED`` — a flaky fixture (network timeout, port collision) emits ``ERROR``
        instead and was silently dropped to the empty-set halt.
        """
        text = _text()
        assert "-rfE" in text

    def test_empty_failing_set_short_circuits_before_retry(self) -> None:
        """A degenerate empty node-id set is rejected before any pytest re-run.

        Guards against widening scope: invoking pytest with zero node-id args
        collects from rootdir/testpaths, which reads as a pass if that wider
        set happens to be green.
        """
        text = _text()
        assert 'if [ -z "$FAILED_IDS" ]; then' in text
        empty_guard_idx = text.index('if [ -z "$FAILED_IDS" ]; then')
        retry_a_idx = text.index("# Retry A: exactly the failing node-ids")
        assert empty_guard_idx < retry_a_idx

    def test_exit_5_treated_as_config_error_not_test_failure(self) -> None:
        """Collection exit 5 is a distinct config error, never routed to retry logic."""
        text = _text()
        assert "SUITE_EXIT -eq 5" in text
        assert "CONFIG ERROR" in text

    def test_parallel_contention_never_labeled_flaky(self) -> None:
        """A failure that reproduces only under ``-n`` gets its own label, not FLAKY.

        Repo CLAUDE.md §Test Workflow: passing serially and failing only under ``-n`` is a real defect, never an xdist
        artifact — conflating it with FLAKY would route a real isolation bug into the mark-and-ignore prompt.
        """
        text = _text()
        assert "PARALLEL_FAIL_SERIAL_PASS" in text
        parallel_idx = text.index("PARALLEL_FAIL_SERIAL_PASS")
        flaky_ask_idx = text.index("FLAKY_DETECTED=true")
        assert parallel_idx < flaky_ask_idx

    def test_retries_never_pass_xdist_args(self) -> None:
        """Both retry re-runs stay serial — never inherit ``$XDIST_ARGS``."""
        text = _text()
        retry_a_start = text.index("# Retry A: exactly the failing node-ids")
        retry_a_block = text[retry_a_start : retry_a_start + 1400]
        retry_a_line = next(
            line for line in retry_a_block.splitlines() if line.strip().startswith('eval "$RUNNER pytest')
        )
        retry_b_start = text.index("Retry B: full-dir, serial")
        retry_b_line = text[retry_b_start : retry_b_start + 300].splitlines()[1]
        assert "XDIST_ARGS" not in retry_a_line
        assert "XDIST_ARGS" not in retry_b_line

    def test_retry_a_quotes_each_node_id_inside_eval(self) -> None:
        """Retry A quotes every failing node-id *inside* the eval string, per id.

        A bare ``eval "$RUNNER pytest --tb=short -v $_FAILED_IDS_SP"`` re-parses each id as a fresh glob pattern under
        zsh — every parametrized node-id contains ``[...]``, so ``NOMATCH`` aborts the whole eval and the failure is
        misread as GENUINE FAILURE (never even runs pytest). Quoting the id inside the eval string, with
        ``$``/backtick/backslash escaped first, keeps it one non-glob-eligible argument.
        """
        text = _text()
        retry_a_start = text.index("# Retry A: exactly the failing node-ids")
        retry_a_block = text[retry_a_start : retry_a_start + 1400]
        assert 'eval "$RUNNER pytest --tb=short -v$_RETRY_ARGS"' in retry_a_block
        assert "_RETRY_ARGS=$(printf '%s\\n' \"$FAILED_IDS\" | while IFS= read -r _id; do" in retry_a_block
        assert 'printf \' "%s"\' "$_esc"' in retry_a_block
        assert '_esc="${_esc//\\$/\\\\\\$}"' in retry_a_block

    def test_runner_invocations_use_eval_for_word_splitting(self) -> None:
        """``$RUNNER`` (a two-token string like "uv run") is never invoked bare.

        Claude Code's Bash tool runs under zsh on macOS (the user's login shell) — zsh does not word-split a bare
        ``"$VAR"``, only unquoted command substitution. A bare ``$RUNNER pytest`` fails with "command not found: uv run
        pytest". Confirmed empirically this session.
        """
        text = _text()
        assert re.search(r"(?<!eval \")\$RUNNER pytest", text) is None
        assert re.search(r"(?<!eval \")\$RUNNER (ruff|mypy)", text) is None

    def test_pipefail_replaces_pipestatus(self) -> None:
        """``set -o pipefail`` + ``$?`` replaces ``${PIPESTATUS[0]}`` code usage — absent under zsh (which has a
        differently-indexed lowercase ``$pipestatus`` instead).

        One explanatory prose mention of the word is fine; the array-indexing syntax itself must be gone.
        """
        text = _text()
        # Exactly one occurrence tolerated: the explanatory prose mention in inline code (`${PIPESTATUS[0]}`) —
        # any second occurrence would be a real code site that regressed back to the broken pattern.
        assert text.count("${PIPESTATUS") == 1
        assert "set -o pipefail" in text

    def test_wide_run_redirects_to_log_not_through_a_pipe(self) -> None:
        """The wide run's exit code comes from ``$?`` after a plain redirect, never a pipe.

        ``pytest | tee log | tail -20`` under ``pipefail`` returns the rightmost *non-zero* command's status — a ``tee``
        failure (unwritable ``$TMPDIR``) can outrank a genuine pytest exit 5, masking the CONFIG ERROR branch. A
        process-substitution form (``> >(tee ...) 2>&1``) was tried and reverted: it runs asynchronously, so a
        ``grep``/``rm`` immediately after could race a still-flushing ``tee``. A plain ``> "$_QS_LOG" 2>&1`` is
        synchronous and pipe-free — ``$?`` is pytest's own exit, unconditionally, and the log is complete before the
        next line runs.
        """
        text = _text()
        assert (
            'eval "$RUNNER pytest <test_dir> $_QS_DOCTEST_ARGS -v --tb=short -rfE $XDIST_ARGS" > "$_QS_LOG" 2>&1'
            in text
        )
        # The async process-substitution form survives only as the explanatory-comment mention
        # above (inside backticks, prefixed by `#`) — never as a live code line.
        assert not any("> >(tee" in line and not line.strip().startswith("#") for line in text.splitlines())
        assert '2>&1 | tee "$_QS_LOG" | tail -20' not in text

    def test_xdist_args_and_doctest_merged_persist_across_fences(self) -> None:
        """``XDIST_ARGS``/``DOCTEST_MERGED`` are written to a CSID sentinel and read back.

        Bash state does not survive between Bash() calls — each fenced block in this file is a separate shell invocation
        (proven by ``set -o pipefail`` being re-issued in every new fence). A value only ever assigned in one fence and
        read bare in a later one is always empty there, silently degrading item 3/6 to a permanent no-op.
        """
        text = _text()
        assert 'echo "$XDIST_ARGS" > "${TMPDIR:-/tmp}/quality-stack-xdist-args-${CSID}"' in text
        assert 'IFS= read -r XDIST_ARGS < "${TMPDIR:-/tmp}/quality-stack-xdist-args-${CSID}"' in text
        assert 'echo "$DOCTEST_MERGED" > "${TMPDIR:-/tmp}/quality-stack-doctest-merged-${CSID}"' in text
        assert text.count('IFS= read -r DOCTEST_MERGED < "${TMPDIR:-/tmp}/quality-stack-doctest-merged-${CSID}"') == 2

    def test_log_removed_on_success_path_too(self) -> None:
        """The session-scoped run log is cleaned up in a trailing fence, not inline in the wide-run fence.

        Round-3 adversarial review (R2) found an inline ``rm -f`` inside the 47-command wide-run fence flagged the whole
        fence ``is_dangerous`` — ``blueprint-allow.js`` re-runs the danger check on the whole submitted block before any
        manifest lookup, so the fence lost its manifest entry and prompted on every call. The fix moves cleanup to a
        standalone one-line fence, isolating the danger cost to that single line.
        """
        text = _text()
        assert 'rm -f "$_QS_LOG"' not in text
        assert 'rm -f "${TMPDIR:-/tmp}/quality-stack-run1-${CSID}.log"' in text
        # the cleanup fence must be its own fence, separate from the wide-run fence containing FLAKY_DETECTED —
        # fence-boundary matching, not a single greedy regex, since two fences both open with "export CSID"
        fences = _bash_fences(text)
        wide_run_fence = next(f for f in fences if "FLAKY_DETECTED=true" in f)
        assert "rm -f" not in wide_run_fence


class TestXdistProbe:
    """Pins the runner-agnostic probe contract — see plan blocker 3, concern 2."""

    def test_probes_through_pytest_help_not_bare_interpreter(self) -> None:
        """Probe uses ``$RUNNER pytest --help``, never a bare ``python -c import xdist``.

        A bare interpreter probe has nothing to hang ``-c`` on for a plain ``pytest`` runner and carries Windows quoting
        risk.
        """
        text = _text()
        assert "numprocesses" in text
        assert "import xdist" not in text

    def test_no_longer_claims_a_runner_detection_sibling(self) -> None:
        """The `policy-sibling` marker is gone — `cc_develop/runner-detection.md`'s copy was deleted (H2).

        Round-2 review (H2) found `runner-detection.md`'s own xdist probe write-only: its `XDIST_AVAILABLE`/`XDIST_ARGS`
        sentinel had zero consumers anywhere in `cc_develop`. Wiring it to this file is forbidden by `plugins/CLAUDE.md`
        §Self-Contained `_shared` (cc_foundry cannot read a cc_develop file), so the dead copy was deleted rather than
        force-wired — this file's probe is no longer duplicated, and the stale `policy-sibling` marker claiming
        otherwise is gone too.
        """
        text = _text()
        assert "policy-sibling:" not in text
        assert "runner-detection.md" in text  # still named in the deletion-history prose above the probe


class TestDoctestFold:
    """Pins the doctest-fold contract — see plan blocker 4."""

    def test_collection_check_runs_before_merge_decision(self) -> None:
        """The clean-collection check precedes the merge, not the other way round."""
        text = _text()
        check_idx = text.index("--collect-only -q")
        merge_flag_idx = text.index('_QS_DOCTEST_ARGS="--doctest-modules')
        assert check_idx < merge_flag_idx

    def test_failed_collection_degrades_to_not_merged_not_a_halt(self) -> None:
        """A failed collection check degrades gracefully — it never exits the stack."""
        text = _text()
        check_block_start = text.index("DOCTEST_MERGED=false")
        check_block_end = text.index('_QS_DOCTEST_ARGS=""')
        check_block = text[check_block_start:check_block_end]
        assert "exit 1" not in check_block

    def test_doctest_failure_never_enters_flaky_retry(self) -> None:
        """A doctest node-id among the failures is a genuine failure, never flaky-eligible.

        Doctests are deterministic examples, not markable pytest test functions — the ``@pytest.mark.flaky`` remediation
        in the FLAKY prompt has no doctest equivalent.
        """
        text = _text()
        assert "DOCTEST_FAILED_IDS" in text
        doctest_check_idx = text.index('if [ -n "$DOCTEST_FAILED_IDS" ]; then')
        retry_a_idx = text.index("# Retry A: exactly the failing node-ids")
        assert doctest_check_idx < retry_a_idx

    def test_report_status_is_three_state(self) -> None:
        """Doctest report status covers not-merged / pass / fail, not a binary pass/fail."""
        text = _text()
        assert "not-merged" in text
        assert "3-state" in text

    def test_doctest_classification_covers_both_pytest_default_patterns(self) -> None:
        """Classification matches pytest's *both* default ``python_files`` patterns.

        pytest's default is ``test_*.py *_test.py`` — a real test in ``foo_test.py`` was misclassified as a doctest and
        hit the no-retry genuine-failure branch under the wrong diagnosis.
        """
        text = _text()
        assert "test_*.py|*_test.py) ;;" in text
        assert 'case "$_base" in\n            test_*) ;;' not in text

    def test_success_path_never_reaches_config_error_or_genuine_failure(self) -> None:
        """The `` -rfE`` classification-and-retry machinery only every runs when the wide run itself failed.

        A regression that moved retry logic outside its ``$SUITE_EXIT -ne 0`` guard would run pytest again on every
        green run — this pins that the whole block stays gated.
        """
        text = _text()
        assert "if [ $SUITE_EXIT -ne 0 ]; then" in text

    def test_collection_fence_has_no_inline_cleanup(self) -> None:
        """The doctest-fold collection check never ``rm -f``s its own log inline.

        Post-round-4 review found this exact fence — never named by any of the four challenger rounds, only found by
        re-scanning every fence for the same shape — combined ``rm -f "$_QS_COLLECT_LOG"`` with the ``eval`` that needs
        manifest coverage: the same class R2 was filed against on the wide-run fence below it in this file. The log's
        path is CSID-deterministic and is simply overwritten by the next run.
        """
        text = _text()
        assert 'rm -f "$_QS_COLLECT_LOG"' not in text
        fences = _bash_fences(text)
        collection_fence = next(f for f in fences if "DOCTEST_MERGED=false" in f)
        assert "rm -f" not in collection_fence


def _fence_unassigned_var_refs(fence_body: str) -> set[str]:
    """Return every variable referenced in a fence that is never assigned or read back in it.

    Mirrors ``test_batch_mode_contract.py``'s checker (cc_develop) — duplicated rather than shared across plugins per
    ``plugins/CLAUDE.md`` §Self-Contained `_shared` (no cross-plugin test-helper imports). Pins the "derived-property
    test" recommended in three consecutive adversarial rounds (M9): a var is only safe to reference if it was assigned
    (``VAR=``), read from a sentinel (``IFS= read -r VAR <``), bound by a ``for VAR in`` loop earlier in the SAME fence,
    or every occurrence on its line carries a default-value expansion (``${VAR:-x}``).
    """
    # `_SHARED` is documented at this file's own top (:5) as "the loading plugin's own skills/_shared, set by the
    # consuming skill" — a cross-cutting convention var, out of scope for this fence-local check.
    # RUNNER deliberately NOT here (round-5 F2): it used to be exempted on a false premise (no
    # sentinel exists) while crossing 5 fence boundaries unpersisted (F3). It now has a real
    # sentinel and every consuming fence reads it back locally — the checker sees that read as an
    # ordinary in-fence assignment, no safe-list entry needed.
    safe_vars = {
        "CSID",
        "TMPDIR",
        "PPID",
        "CLAUDE_CODE_SESSION_ID",
        "IFS",
        "PYTEST_CMD",
        "TEST_CMD",
        "PWD",
        "_SHARED",
    }
    assign_re = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=")
    read_re = re.compile(r"^[ \t]*IFS=\s*read\s+-r\s+([A-Za-z_][A-Za-z0-9_]*)\s*<")
    # `while IFS= read -r VAR; do` (reading a piped stream, not a sentinel file — no trailing `<`).
    # Not anchored to segment start: the common shape is `VAR=$(... | while IFS= read -r _x; do`.
    while_read_re = re.compile(r"while\s+IFS=\s*read\s+-r\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    for_re = re.compile(r"^[ \t]*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
    ref_re = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
    # One-line `if ...; then VAR=x; else VAR=y; fi` — the keyword sits before VAR= on the same
    # segment after `;`-splitting, breaking every anchored regex above (round-5 F3 fix surfaced
    # this: the detection fence's own `if ...; then RUNNER="uv run"; else RUNNER="python -m"; fi`
    # was flagged as an unassigned reference to the very var it defines).
    leading_keyword_re = re.compile(r"^[ \t]*(?:then|else|do)\s+")

    def strip_trailing_comment(segment: str) -> str:
        """Strip a genuine trailing ``# ...`` comment, quote-aware — a prose comment mentioning ``$VAR`` is not a
        reference."""
        in_squote = in_dquote = False
        for i, ch in enumerate(segment):
            if ch == "'" and not in_dquote:
                in_squote = not in_squote
            elif ch == '"' and not in_squote:
                in_dquote = not in_dquote
            elif ch == "#" and not in_squote and not in_dquote and (i == 0 or segment[i - 1].isspace()):
                return segment[:i]
        return segment

    def defended(var: str, segment: str) -> bool:
        bare = re.findall(r"\$\{?" + re.escape(var) + r"(?![A-Za-z0-9_])", segment)
        defended_forms = re.findall(r"\$\{" + re.escape(var) + r":[-=?+]", segment)
        return len(bare) == len(defended_forms)

    available = set(safe_vars)
    violations: set[str] = set()
    for line in fence_body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # `;`-split so a compound one-liner (`X=$(...); [ -n "$X" ] || X=fallback`) sees its own
        # earlier assignment before the later reference — a whole-line check would flag $X as
        # unassigned on its very first (assigning) occurrence.
        for segment in line.split(";"):
            ref_segment = strip_trailing_comment(segment)
            for m in ref_re.finditer(ref_segment):
                var = m.group(1)
                if len(var) > 1 and not var.isdigit() and var not in available and not defended(var, ref_segment):
                    violations.add(var)
            anchor_segment = leading_keyword_re.sub("", segment, count=1)
            # not mutually exclusive — `VAR=$(... | while IFS= read -r _x; do` binds both VAR
            # (outer assignment) and _x (inner while-read loop var) in the same segment
            if read_match := read_re.match(anchor_segment):
                available.add(read_match.group(1))
            if while_read_match := while_read_re.search(segment):
                available.add(while_read_match.group(1))
            if for_match := for_re.match(anchor_segment):
                available.add(for_match.group(1))
            if assign_match := assign_re.match(anchor_segment):
                available.add(assign_match.group(1))
    return violations


class TestDerivedPropertyVariables:
    """The M9 derived-property test — recommended in three consecutive rounds, never written until now."""

    def test_every_fence_variable_is_assigned_or_read_back_in_that_fence(self) -> None:
        """No fence in quality-stack.md consumes a variable nothing assigns or reads back.

        The exact test that would have caught R2's inline ``rm -f`` prose claims and the doctest-fold collection fence's
        identical shape before a challenger — or a post-loop re-scan — had to find them by hand.
        """
        text = _text()
        fences = _bash_fences(text)
        for i, fence in enumerate(fences):
            violations = _fence_unassigned_var_refs(fence)
            assert not violations, f"quality-stack.md fence #{i} references unassigned var(s) {violations}:\n{fence}"
