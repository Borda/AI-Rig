"""Contract tests over ``quality-stack.md``'s flaky-retry and xdist-probe text.

Static assertions over the markdown source, not a bash-execution harness — this skill's bash blocks run only inside a
live Claude Code session, so pinning the specific patterns is the available regression guard, the way
``test_check_codemap_guard.py`` pins its own bash-preamble invariants. A future edit that silently reintroduces the old
blind double-retry, or drops the degenerate-empty-set guard, fails collection here instead of shipping.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_FOUNDRY = Path(__file__).resolve().parent.parent.parent
_QUALITY_STACK = _FOUNDRY / "skills" / "_shared" / "quality-stack.md"

_requires_bash = pytest.mark.skipif(not _bash_runs_posix_script(), reason="Bash cannot execute POSIX scripts")
_requires_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")

#: Shells the shipped runner ``case`` line must behave identically in (Claude Code's Bash tool is zsh on macOS).
_SHELLS = [
    pytest.param("bash", marks=_requires_bash, id="bash"),
    pytest.param("zsh", marks=_requires_zsh, id="zsh"),
]


def _bash_fences(text: str) -> list[str]:
    """Return every fenced ```bash block's body, matched by fence boundary (never a naive greedy span).

    The closing marker must carry the SAME leading indent as the opening one: an unanchored scan silently merges two
    fences when an indented open marker's own indented close is skipped past in favour of a later unindented one.
    quality-stack.md has no indented fences today, but the check stays fence-exact.
    """
    fences: list[str] = []
    body: list[str] | None = None
    indent = ""
    for line in text.split("\n"):
        if body is None:
            stripped = line.lstrip(" \t")
            if stripped == "```bash":
                indent = line[: len(line) - len(stripped)]
                body = []
        elif line.startswith(indent + "```"):
            fences.append("\n".join(body))
            body = None
        else:
            body.append(line)
    return fences


@pytest.fixture(scope="module", name="text")
def _text() -> str:
    """Canonical quality-stack.md content, read once (immutable for the module)."""
    return _QUALITY_STACK.read_text(encoding="utf-8")


@pytest.fixture(scope="module", name="fences")
def _fences(text: str) -> list[str]:
    """Every fenced bash block of quality-stack.md."""
    return _bash_fences(text)


@pytest.fixture(scope="module", name="runner_case_line")
def _runner_case_line(text: str) -> str:
    """Return the closed ``case`` line every runner fence carries, exactly as shipped."""
    return next(line for line in text.splitlines() if line.startswith('case "$RUNNER" in'))


def _retry_a_block(text: str) -> str:
    """Return the 1400 characters following the ``# Retry A`` marker — the retry-A command and its array loop."""
    start = text.index("# Retry A: exactly the failing node-ids")
    return text[start : start + 1400]


class TestFlakyRetryRedesign:
    """Pins the redesigned retry contract — see plan blockers 2, 3, 7."""

    def test_old_blind_double_retry_loop_removed(self, text: str) -> None:
        """The old ``for _i in 2 3`` blind whole-directory retry loop is gone.

        That loop re-ran the full test directory twice on any failure with no node-id narrowing — the largest confirmed
        cost source in the original investigation. Its return here would silently undo the redesign.
        """
        assert "for _i in 2 3" not in text

    def test_failing_node_ids_captured_from_full_untruncated_log(self, text: str) -> None:
        """Failing node-ids (FAILED and ERROR) come from a full log file, not a tail-capped stream.

        Scenario: the wide run writes a complete log and the ids are grepped from it, so a long failure list is never
        cut off by a ``tail``.
        """
        assert "FAILED_IDS" in text
        assert "grep -E '^(FAILED|ERROR) '" in text

    def test_node_id_extraction_anchors_on_id_structure_not_dash_search(self, text: str) -> None:
        """Node-id extraction matches pytest's own node-id grammar, not a `` - `` text search.

        ``awk '{print $2}'`` truncated at the first whitespace. A first fix (greedy ``(.*) - .*$``, rightmost split)
        over-captured whenever the *reason* itself contained `` - `` (e.g. ``assert 1 == 2 - extra``) — verified: it
        swallowed the reason's own dash into the id. The id is structurally a whitespace-free prefix plus at most one
        bracket group that may hold one nested level (``path::name[params]`` or ``name[[a] b]`` — only ``params`` may
        contain spaces/dashes); anchoring there is correct regardless of what the free-text reason contains, including
        a reason that itself has brackets. ``[^[:space:]]`` not ``\\S`` — BSD sed (macOS) has no ``\\S``.
        """
        assert "| awk '{print $2}'" not in text
        assert "(.*) - .*$/\\2/" not in text  # the rejected greedy-dash-search form
        assert "sed -E 's/^(FAILED|ERROR) ([^[:space:]]+(\\[([^][]|\\[[^]]*\\])*\\])?).*$/\\2/'" in text

    def test_error_lines_retried_alongside_failed(self, text: str) -> None:
        """Fixture/collection ``ERROR`` lines feed the same retry path as ``FAILED`` lines.

        ``-rf`` alone reports only ``FAILED`` — a flaky fixture (network timeout, port collision) emits ``ERROR``
        instead and was silently dropped to the empty-set halt.
        """
        assert "-rfE" in text

    def test_empty_failing_set_short_circuits_before_retry(self, text: str) -> None:
        """A degenerate empty node-id set is rejected before any pytest re-run.

        Guards against widening scope: invoking pytest with zero node-id args collects from rootdir/testpaths, which
        reads as a pass if that wider set happens to be green.
        """
        empty_guard_idx = text.index('if [ -z "$FAILED_IDS" ]; then')
        retry_a_idx = text.index("# Retry A: exactly the failing node-ids")
        assert empty_guard_idx < retry_a_idx

    def test_exit_5_treated_as_config_error_not_test_failure(self, text: str) -> None:
        """Collection exit 5 is a distinct config error, never routed to retry logic.

        Scenario: pytest exit code 5 (no tests collected) is a configuration problem, so the stack reports it as one
        instead of retrying a run that can never pass.
        """
        assert "SUITE_EXIT -eq 5" in text
        assert "CONFIG ERROR" in text

    def test_parallel_contention_never_labeled_flaky(self, text: str) -> None:
        """A failure that reproduces only under ``-n`` gets its own label, not FLAKY.

        Repo CLAUDE.md §Test Workflow: passing serially and failing only under ``-n`` is a real defect, never an xdist
        artifact — conflating it with FLAKY would route a real isolation bug into the mark-and-ignore prompt.
        """
        parallel_idx = text.index("PARALLEL_FAIL_SERIAL_PASS")
        flaky_ask_idx = text.index("FLAKY_DETECTED=true")
        assert parallel_idx < flaky_ask_idx

    def test_retries_never_pass_xdist_args(self, text: str) -> None:
        """Both retry re-runs stay serial — never inherit ``$XDIST_ARGS``.

        Scenario: retry A re-runs exactly the failing node-ids and retry B re-runs the full directory. Either one under
        xdist would reintroduce the contention that retrying is meant to rule out.
        """
        retry_a_line = next(
            line for line in _retry_a_block(text).splitlines() if line.strip().startswith('"${R[@]}" pytest')
        )
        retry_b_start = text.index("Retry B: full-dir, serial")
        retry_b_line = text[retry_b_start : retry_b_start + 300].splitlines()[1]
        assert "XDIST" not in retry_a_line
        assert "XDIST" not in retry_b_line

    def test_retry_a_passes_each_node_id_as_one_array_element(self, text: str) -> None:
        """Retry A collects every failing node-id into an array, one argument per id.

        A bare word-split of the id list would re-parse each id as a fresh glob pattern under zsh — every parametrized
        node-id contains ``[...]``, so ``NOMATCH`` aborts the command and the failure is misread as GENUINE FAILURE
        (pytest never runs). Array elements expand quoted, so no id is split or globbed. The loop reads through process
        substitution because a piped ``while`` runs in a subshell and would lose the array.
        """
        retry_a_block = _retry_a_block(text)
        assert '"${R[@]}" pytest --tb=short -v "${RETRY[@]}"' in retry_a_block
        assert 'RETRY+=("$_id")' in retry_a_block
        assert "done < <(printf '%s\\n' \"$FAILED_IDS\")" in retry_a_block
        assert "eval" not in retry_a_block

    def test_runner_invocations_use_the_closed_case_array(
        self, text: str, fences: list[str], runner_case_line: str
    ) -> None:
        """``$RUNNER`` (a two-token string like "uv run") is mapped to an array and never run as a string.

        Claude Code's Bash tool runs under zsh on macOS (the user's login shell) — zsh does not word-split a bare
        ``"$VAR"``, so a bare ``$RUNNER pytest`` fails with "command not found: uv run pytest". ``eval`` would work but
        runs whatever a planted session file holds; the closed ``case`` accepts only the three detected literals.
        """
        runner_fences = [f for f in fences if '"${R[@]}"' in f]
        assert re.search(r"\beval\b", text) is None
        assert re.search(r"\$RUNNER (pytest|ruff|mypy)", text) is None
        assert len(runner_fences) >= 6
        assert all(runner_case_line in f for f in runner_fences)

    def test_pipefail_replaces_pipestatus(self, text: str) -> None:
        """``set -o pipefail`` plus ``$?`` replaces ``${PIPESTATUS[0]}`` code usage.

        ``PIPESTATUS`` is absent under zsh, which has a differently-indexed lowercase ``$pipestatus`` instead. Exactly
        one occurrence is tolerated: the explanatory prose mention in inline code. A second would be a real code site
        that regressed back to the broken pattern.
        """
        assert text.count("${PIPESTATUS") == 1
        assert "set -o pipefail" in text

    def test_wide_run_redirects_to_log_not_through_a_pipe(self, text: str) -> None:
        """The wide run's exit code comes from ``$?`` after a plain redirect, never a pipe.

        ``pytest | tee log | tail -20`` under ``pipefail`` returns the rightmost *non-zero* command's status — a ``tee``
        failure (unwritable ``$TMPDIR``) can outrank a genuine pytest exit 5, masking the CONFIG ERROR branch. A
        process-substitution form (``> >(tee ...) 2>&1``) was tried and reverted: it runs asynchronously, so a
        ``grep``/``rm`` immediately after could race a still-flushing ``tee``. A plain ``> "$_QS_LOG" 2>&1`` is
        synchronous and pipe-free — ``$?`` is pytest's own exit, unconditionally, and the log is complete before the
        next line runs. The async form survives only as an explanatory comment, never as a live code line.
        """
        wide_run = (
            '"${R[@]}" pytest <test_dir> "${_QS_DOCTEST_ARGS[@]}" -v --tb=short -rfE "${XDIST[@]}" > "$_QS_LOG" 2>&1'
        )
        assert wide_run in text
        assert not any("> >(tee" in line and not line.strip().startswith("#") for line in text.splitlines())
        assert '2>&1 | tee "$_QS_LOG" | tail -20' not in text

    def test_xdist_args_and_doctest_merged_persist_across_fences(self, text: str) -> None:
        """``XDIST_ARGS``/``DOCTEST_MERGED`` are written to a CSID sentinel and read back.

        Bash state does not survive between Bash() calls — each fenced block in this file is a separate shell invocation
        (proven by ``set -o pipefail`` being re-issued in every new fence). A value only ever assigned in one fence and
        read bare in a later one is always empty there, silently degrading item 3/6 to a permanent no-op.
        """
        assert 'echo "$XDIST_ARGS" > "${TMPDIR:-/tmp}/quality-stack-xdist-args-${CSID}"' in text
        assert 'IFS= read -r XDIST_ARGS < "${TMPDIR:-/tmp}/quality-stack-xdist-args-${CSID}"' in text
        assert 'echo "$DOCTEST_MERGED" > "${TMPDIR:-/tmp}/quality-stack-doctest-merged-${CSID}"' in text
        assert text.count('IFS= read -r DOCTEST_MERGED < "${TMPDIR:-/tmp}/quality-stack-doctest-merged-${CSID}"') == 2

    def test_log_removed_on_success_path_too(self, text: str, fences: list[str]) -> None:
        """The session-scoped run log is cleaned up in a trailing fence, not inline in the wide-run fence.

        Round-3 adversarial review (R2) found an inline ``rm -f`` inside the 47-command wide-run fence flagged the whole
        fence ``is_dangerous`` — ``blueprint-allow.js`` re-runs the danger check on the whole submitted block before any
        manifest lookup, so the fence lost its manifest entry and prompted on every call. The fix moves cleanup to a
        standalone one-line fence, isolating the danger cost to that single line. The cleanup fence must be its own
        fence, separate from the wide-run fence containing FLAKY_DETECTED — fence-boundary matching, since two fences
        both open with "export CSID".
        """
        wide_run_fence = next(f for f in fences if "FLAKY_DETECTED=true" in f)
        assert 'rm -f "$_QS_LOG"' not in text
        assert 'rm -f "${TMPDIR:-/tmp}/quality-stack-run1-${CSID}.log"' in text
        assert "rm -f" not in wide_run_fence


class TestRunnerCaseExecution:
    """Runs the shipped ``case`` line in a real shell: a planted runner value never reaches a command."""

    @staticmethod
    def _run(shell: str, case_line: str, runner: str, workdir: Path) -> subprocess.CompletedProcess[str]:
        """Run the shipped case line, then the array call, in ``workdir`` with ``RUNNER`` preset."""
        script = f'RUNNER={shlex.quote(runner)}\n{case_line}\nprintf "[%s]" "${{R[@]}}"\n'
        return subprocess.run(
            [shell, "-c", script], capture_output=True, text=True, timeout=30, check=False, cwd=workdir
        )

    @pytest.mark.parametrize("shell", _SHELLS)
    @pytest.mark.parametrize(
        ("runner", "expected"),
        [
            pytest.param("uv run", "[uv][run]", id="uv"),
            pytest.param("poetry run", "[poetry][run]", id="poetry"),
            pytest.param("python -m", "[python][-m]", id="python"),
        ],
    )
    def test_detected_runner_becomes_separate_words(
        self, shell: str, runner: str, expected: str, runner_case_line: str, tmp_path: Path
    ) -> None:
        """Each detected runner expands to one array element per word in bash and zsh alike.

        Scenario: the real shell runs the shipped ``case`` line with one of the three accepted runner literals and
        prints the resulting array, so a word-splitting difference between shells would show up here.
        """
        proc = self._run(shell, runner_case_line, runner, tmp_path)

        assert (proc.returncode, proc.stdout) == (0, expected)

    @pytest.mark.parametrize("shell", _SHELLS)
    @pytest.mark.parametrize(
        "planted",
        [
            pytest.param("uv run; touch pwned", id="semicolon"),
            pytest.param("uv run $(touch pwned)", id="substitution"),
            pytest.param("", id="empty"),
        ],
    )
    def test_planted_runner_is_blocked_and_runs_nothing(
        self, shell: str, planted: str, runner_case_line: str, tmp_path: Path
    ) -> None:
        """A session file holding anything but the three literals exits 1 and executes nothing.

        Scenario: the runner value carries a command separator, a command substitution, or nothing at all. The shipped
        ``case`` must print the BLOCKED message and never run ``touch pwned``.
        """
        proc = self._run(shell, runner_case_line, planted, tmp_path)

        assert proc.returncode == 1
        assert "! BLOCKED" in proc.stdout
        assert not (tmp_path / "pwned").exists()


class TestXdistProbe:
    """Pins the runner-agnostic probe contract — see plan blocker 3, concern 2."""

    def test_probes_through_pytest_help_not_bare_interpreter(self, text: str) -> None:
        """Probe uses ``$RUNNER pytest --help``, never a bare ``python -c import xdist``.

        A bare interpreter probe has nothing to hang ``-c`` on for a plain ``pytest`` runner and carries Windows quoting
        risk.
        """
        assert "numprocesses" in text
        assert "import xdist" not in text

    def test_no_longer_claims_a_runner_detection_sibling(self, text: str) -> None:
        """The `policy-sibling` marker is gone — `cc_develop/runner-detection.md`'s copy was deleted (H2).

        Round-2 review (H2) found `runner-detection.md`'s own xdist probe write-only: its `XDIST_AVAILABLE`/`XDIST_ARGS`
        sentinel had zero consumers anywhere in `cc_develop`. Wiring it to this file is forbidden by `plugins/CLAUDE.md`
        §Self-Contained `_shared` (cc_foundry cannot read a cc_develop file), so the dead copy was deleted rather than
        force-wired — this file's probe is no longer duplicated, and the stale `policy-sibling` marker claiming
        otherwise is gone too.
        """
        assert "policy-sibling:" not in text
        assert "runner-detection.md" in text  # still named in the deletion-history prose above the probe


class TestDoctestFold:
    """Pins the doctest-fold contract — see plan blocker 4."""

    def test_collection_check_runs_before_merge_decision(self, text: str) -> None:
        """The clean-collection check precedes the merge, not the other way round.

        Scenario: doctests are folded into the wide run only after ``--collect-only`` proves they collect cleanly.
        """
        check_idx = text.index("--collect-only -q")
        merge_flag_idx = text.index("_QS_DOCTEST_ARGS=(--doctest-modules")
        assert check_idx < merge_flag_idx

    def test_failed_collection_degrades_to_not_merged_not_a_halt(self, text: str, runner_case_line: str) -> None:
        """A failed collection check degrades gracefully — it never exits the stack.

        Scenario: the closed runner ``case`` legitimately exits 1 on a planted runner, so that line is removed from the
        collection-check block before asserting that nothing else in it can halt the stack.
        """
        check_block_start = text.index("DOCTEST_MERGED=false")
        check_block_end = text.index("_QS_DOCTEST_ARGS=()")
        check_block = text[check_block_start:check_block_end].replace(runner_case_line, "")
        assert "exit 1" not in check_block

    def test_doctest_failure_never_enters_flaky_retry(self, text: str) -> None:
        """A doctest node-id among the failures is a genuine failure, never flaky-eligible.

        Doctests are deterministic examples, not markable pytest test functions — the ``@pytest.mark.flaky`` remediation
        in the FLAKY prompt has no doctest equivalent.
        """
        assert "DOCTEST_FAILED_IDS" in text
        doctest_check_idx = text.index('if [ -n "$DOCTEST_FAILED_IDS" ]; then')
        retry_a_idx = text.index("# Retry A: exactly the failing node-ids")
        assert doctest_check_idx < retry_a_idx

    def test_report_status_is_three_state(self, text: str) -> None:
        """Doctest report status covers not-merged / pass / fail, not a binary pass/fail.

        Scenario: a run where doctests were never folded in must be distinguishable from one where they passed.
        """
        assert "not-merged" in text
        assert "3-state" in text

    def test_separate_doctest_pass_halts_on_failure(self, fences: list[str]) -> None:
        """The degraded separate doctest pass acts on its exit code instead of only recording it.

        Scenario: collection failed, so doctests run as their own pass. A failing doctest there (pytest exit 1) used to
        be captured in ``DOCTEST_EXIT`` and never read, so the stack continued and reported ``not-merged``. Exit 5
        (module has no doctests) stays a pass; every other non-zero exit halts.
        """
        doctest_fence = next(f for f in fences if 'if [ "$DOCTEST_MERGED" != "true" ]; then' in f)
        case_idx = doctest_fence.index("case $DOCTEST_EXIT in")
        arms = doctest_fence[case_idx:]
        assert doctest_fence.index("DOCTEST_EXIT=$?") < case_idx
        assert "0|5) ;;" in arms
        assert "1)\n" in arms
        assert "*)\n" in arms
        assert arms.count("exit 1") == 2
        assert arms.count("Quality stack halted — do not proceed further") == 2

    def test_classification_covers_both_pytest_default_patterns(self, text: str) -> None:
        """Classification matches pytest's *both* default ``python_files`` patterns.

        pytest's default is ``test_*.py *_test.py`` — a real test in ``foo_test.py`` was misclassified as a doctest and
        hit the no-retry genuine-failure branch under the wrong diagnosis.
        """
        assert "test_*.py|*_test.py) ;;" in text
        assert 'case "$_base" in\n            test_*) ;;' not in text

    def test_retry_machinery_gated_on_failed_wide_run(self, text: str) -> None:
        """The `` -rfE`` classification-and-retry machinery only ever runs when the wide run itself failed.

        A regression that moved retry logic outside its ``$SUITE_EXIT -ne 0`` guard would run pytest again on every
        green run — this pins that the whole block stays gated.
        """
        assert "if [ $SUITE_EXIT -ne 0 ]; then" in text

    def test_collection_fence_has_no_inline_cleanup(self, text: str, fences: list[str]) -> None:
        """The doctest-fold collection check never ``rm -f``s its own log inline.

        Post-round-4 review found this exact fence — never named by any of the four challenger rounds, only found by
        re-scanning every fence for the same shape — combined ``rm -f "$_QS_COLLECT_LOG"`` with the runner call that
        needs manifest coverage: the same class R2 was filed against on the wide-run fence below it in this file. The
        log's path is CSID-deterministic and is simply overwritten by the next run.
        """
        collection_fence = next(f for f in fences if "DOCTEST_MERGED=false" in f)
        assert 'rm -f "$_QS_COLLECT_LOG"' not in text
        assert "rm -f" not in collection_fence


def _fence_unassigned_var_refs(fence_body: str) -> set[str]:
    """Return every variable referenced in a fence that is never assigned or read back in it.

    Mirrors ``test_batch_mode_contract.py``'s checker (cc_develop) — duplicated rather than shared across plugins per
    ``plugins/CLAUDE.md`` §Self-Contained `_shared` (no cross-plugin test-helper imports). A var is only safe to
    reference if it was assigned (``VAR=``), read from a sentinel (``IFS= read -r VAR <``), bound by a ``for VAR in``
    loop earlier in the SAME fence, or every occurrence on its line carries a default-value expansion (``${VAR:-x}``).
    """
    # `_SHARED` is documented at this file's own top as "the loading plugin's own skills/_shared, set by the consuming
    # skill" — a cross-cutting convention var, out of scope for this fence-local check.
    # RUNNER deliberately NOT here: it used to be exempted on a false premise (no sentinel exists) while crossing 5
    # fence boundaries unpersisted. It now has a real sentinel and every consuming fence reads it back locally — the
    # checker sees that read as an ordinary in-fence assignment, no safe-list entry needed.
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
    # One-line `if ...; then VAR=x; else VAR=y; fi` — the keyword sits before VAR= on the same segment after
    # `;`-splitting, breaking every anchored regex above (the detection fence's own
    # `if ...; then RUNNER="uv run"; else RUNNER="python -m"; fi` was flagged as an unassigned reference to the very
    # var it defines).
    leading_keyword_re = re.compile(r"^[ \t]*(?:then|else|do)\s+")

    def strip_trailing_comment(segment: str) -> str:
        """Strip a genuine trailing ``# ...`` comment, quote-aware — a prose comment naming ``$VAR`` is no reference."""
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
        """Report whether every reference to ``var`` in ``segment`` carries a ``${VAR:-x}``-style default."""
        bare = re.findall(r"\$\{?" + re.escape(var) + r"(?![A-Za-z0-9_])", segment)
        defended_forms = re.findall(r"\$\{" + re.escape(var) + r":[-=?+]", segment)
        return len(bare) == len(defended_forms)

    available = set(safe_vars)
    violations: set[str] = set()
    for line in fence_body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # `;`-split so a compound one-liner (`X=$(...); [ -n "$X" ] || X=fallback`) sees its own earlier assignment
        # before the later reference — a whole-line check would flag $X as unassigned on its first (assigning) use.
        for segment in line.split(";"):
            ref_segment = strip_trailing_comment(segment)
            for m in ref_re.finditer(ref_segment):
                var = m.group(1)
                if len(var) > 1 and not var.isdigit() and var not in available and not defended(var, ref_segment):
                    violations.add(var)
            anchor_segment = leading_keyword_re.sub("", segment, count=1)
            # not mutually exclusive — `VAR=$(... | while IFS= read -r _x; do` binds both VAR (outer assignment) and _x
            # (inner while-read loop var) in the same segment
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
    """The derived-property test recommended in three consecutive adversarial rounds."""

    def test_every_fence_variable_is_assigned_or_read_back_in_that_fence(self, fences: list[str]) -> None:
        """No fence in quality-stack.md consumes a variable nothing assigns or reads back.

        Scenario: every bash fence is a separate shell invocation, so a variable used without an in-fence assignment or
        sentinel read-back is empty at run time. This is the check that would have caught the inline ``rm -f`` and the
        doctest-fold collection fence shape before a challenger — or a post-loop re-scan — had to find them by hand.
        """
        unassigned = {i: refs for i, fence in enumerate(fences) if (refs := _fence_unassigned_var_refs(fence))}
        assert not unassigned, f"quality-stack.md fences reference unassigned vars (fence index -> vars): {unassigned}"
