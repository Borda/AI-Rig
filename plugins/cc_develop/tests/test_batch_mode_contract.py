"""Contract tests over ``_shared/batch-mode.md`` and its wiring into feature/refactor.

Static assertions over the markdown source — the same trade-off as ``test_quality_stack_retry_contract.py`` in the
foundry plugin. Pins the design facts that resolved the challenger's blocker on item 7: a real per-edit revert mechanism
(not a nonexistent precedent), a non-overlap predicate over both files and test sets, and edits-processed cap accounting
that leaves the no-batch path untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DEVELOP = Path(__file__).resolve().parent.parent
_BATCH_MODE = _DEVELOP / "skills" / "_shared" / "batch-mode.md"
_FEATURE_SKILL = _DEVELOP / "skills" / "feature" / "SKILL.md"
_REFACTOR_SKILL = _DEVELOP / "skills" / "refactor" / "SKILL.md"


def _text(path: Path) -> str:
    """Read a skill/shared-doc file once per call."""
    return path.read_text(encoding="utf-8")


def _bash_fences(text: str) -> list[str]:
    """Return every fenced ```bash block's body, fence-boundary matched (never a naive greedy span).

    Anchors the closing marker to the SAME leading indent as the opening one (round-5 M9 file-scope extension bug):
    feature/SKILL.md and refactor/SKILL.md both nest ```bash fences inside numbered-list items, indented 3 spaces. A
    prior version of this regex (no indent-anchoring) matched an indented open marker but then skipped past its own
    indented close marker to the next UNINDENTED ``` anywhere below, silently merging two unrelated fences into one.
    """
    return [m.group(2) for m in re.finditer(r"^([ \t]*)```bash\n(.*?)\n\1```", text, re.MULTILINE | re.DOTALL)]


# Session-level vars established once by runner-detection.md / the calling skill's own earlier
# steps, out of scope for this fence-local check — never sentinel-tracked by design, and never the
# signature this check targets (H3/M1/M2/M8/N9: "fence consumes a var nothing assigns or reads back
# IN THAT FENCE"). CSID/TMPDIR/IFS are shell/env builtins, not skill-derived state. RUNNER
# deliberately NOT here (round-5 F2/F3 — same fix as the foundry copy): it now carries its own
# sentinel and every consuming fence reads it back locally. ARGUMENTS is Claude Code's own
# skill-invocation substitution — the harness splices the literal argument text into every fence
# before Bash() ever sees it, so it is never shell state and never unassigned in a live run.
# MODULE_PATH is an agent-resolved pre-fence value (feature/SKILL.md's own comment documents the
# resolution the calling agent performs before invoking that fence) — same class as the documented
# `<test_dir>`/`<target_module>` residual risk, not sentinel-tracked by design.
_FENCE_LOCAL_SAFE_VARS = frozenset(
    {
        "CSID",
        "TMPDIR",
        "PPID",
        "CLAUDE_CODE_SESSION_ID",
        "IFS",
        "PYTEST_CMD",
        "TEST_CMD",
        "PWD",
        "ARGUMENTS",
        "MODULE_PATH",
    }
)
_ASSIGN_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=")
_READ_RE = re.compile(r"^[ \t]*IFS=\s*read\s+-r\s+([A-Za-z_][A-Za-z0-9_]*)\s*<")
# `while IFS= read -r VAR; do` (reading a piped stream, not a sentinel file — no trailing `<`). Not
# anchored to line start: the common shape is `VAR=$(... | while IFS= read -r _x; do`.
_WHILE_READ_RE = re.compile(r"while\s+IFS=\s*read\s+-r\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_FOR_RE = re.compile(r"^[ \t]*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# One-line `if ...; then VAR=x; else VAR=y; fi` — the `then`/`else` keyword sits before VAR= on the
# same segment after `;`-splitting, breaking the line-start anchor every assignment regex uses.
# Stripped only for the anchored assign/read/for checks below, never for reference scanning.
_LEADING_KEYWORD_RE = re.compile(r"^[ \t]*(?:then|else|do)\s+")
# `eval "$(python .../parse-skill-flags.py --flags ... --value-flags a,b ...)"` is this codebase's
# standard idiom for binding CLEAN_ARGS and one VALUE_<NAME> per --value-flags entry via eval — a
# real, mechanical assignment the checker can't see through `eval "$(...)"` without recognizing the
# producing script by name.
_EVAL_PARSE_SKILL_FLAGS_RE = re.compile(r"eval\s+\"\$\(python.*parse-skill-flags\.py")
_VALUE_FLAGS_RE = re.compile(r"--value-flags\s+(\S+)")
# Same idiom, `derive_codemap_target.py`'s own two-variable eval contract.
_EVAL_DERIVE_CODEMAP_TARGET_RE = re.compile(r"eval\s+\"\$\(python.*derive_codemap_target\.py")


def _strip_trailing_comment(segment: str) -> str:
    """Strip a genuine trailing ``# ...`` comment, quote-aware.

    A prior version scanned the whole line including trailing prose comments for ``$VAR`` references — a comment like
    ``# foundry--quality-stack.md loads its siblings from $_SHARED`` flagged ``_SHARED`` as an unassigned reference,
    when the only real reference was the assignment on the same line. Only a whole-line comment (``line.strip()``
    starting with ``#``) was previously stripped; a trailing comment on a code line was not.
    """
    in_squote = in_dquote = False
    for i, ch in enumerate(segment):
        if ch == "'" and not in_dquote:
            in_squote = not in_squote
        elif ch == '"' and not in_squote:
            in_dquote = not in_dquote
        elif ch == "#" and not in_squote and not in_dquote and (i == 0 or segment[i - 1].isspace()):
            return segment[:i]
    return segment


def _var_is_defended(var: str, line: str) -> bool:
    """Return True when every occurrence of ``var`` on ``line`` uses a default-value expansion.

    ``${VAR:-x}``/``${VAR:=x}``/``${VAR:?}``/``${VAR:+x}`` survive an unset var by design — the same
    exemption ``check_bash_persistence.py`` (Check 41) applies for cross-block references.
    """
    bare_refs = re.findall(r"\$\{?" + re.escape(var) + r"(?![A-Za-z0-9_])", line)
    defended_refs = re.findall(r"\$\{" + re.escape(var) + r":[-=?+]", line)
    return len(bare_refs) == len(defended_refs)


def _fence_unassigned_var_refs(fence_body: str) -> set[str]:
    """Return every variable referenced in a fence that is never assigned or read back in it.

    Pins the "derived-property test" recommended in three consecutive adversarial rounds (M9): a var is only ever safe
    to reference if it was assigned (``VAR=``), read from a sentinel (``IFS= read -r VAR <``), bound by a ``for VAR in``
    loop earlier in the SAME fence, or every occurrence on its line carries a default-value expansion (``${VAR:-x}``) —
    never left to "the calling procedure" or a prior fence's bash state, which does not survive across ``Bash()`` calls.
    """
    available = set(_FENCE_LOCAL_SAFE_VARS)
    violations: set[str] = set()
    for line in fence_body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # `eval "$(python .../parse-skill-flags.py ...)"` / `.../derive_codemap_target.py` bind
        # CLEAN_ARGS/VALUE_*/TARGET_MODULE/TARGET_FN via eval, invisible to the assign/read regexes
        # below — recognized by producing-script name, once per line, before per-segment scanning.
        if _EVAL_PARSE_SKILL_FLAGS_RE.search(line):
            available.add("CLEAN_ARGS")
            for value_flags in _VALUE_FLAGS_RE.findall(line):
                for flag_name in value_flags.split(","):
                    available.add("VALUE_" + flag_name.upper())
        if _EVAL_DERIVE_CODEMAP_TARGET_RE.search(line):
            available.add("TARGET_MODULE")
            available.add("TARGET_FN")
        # `;`-split so a compound one-liner (`X=$(...); [ -n "$X" ] || X=fallback`) sees its own
        # earlier assignment before the later reference — a whole-line check would flag $X as
        # unassigned on its very first (assigning) occurrence.
        for segment in line.split(";"):
            ref_segment = _strip_trailing_comment(segment)
            for m in _VAR_REF_RE.finditer(ref_segment):
                var = m.group(1)
                if (
                    len(var) > 1
                    and not var.isdigit()
                    and var not in available
                    and not _var_is_defended(var, ref_segment)
                ):
                    violations.add(var)
            # `then VAR=x`/`else VAR=y` on a one-line if — strip the leading keyword only for the
            # anchored assign/read/for checks, which the leading keyword would otherwise defeat.
            anchor_segment = _LEADING_KEYWORD_RE.sub("", segment, count=1)
            # not mutually exclusive — `VAR=$(... | while IFS= read -r _x; do` binds both VAR
            # (outer assignment) and _x (inner while-read loop var) in the same segment
            if read_match := _READ_RE.match(anchor_segment):
                available.add(read_match.group(1))
            if while_read_match := _WHILE_READ_RE.search(segment):
                available.add(while_read_match.group(1))
            if for_match := _FOR_RE.match(anchor_segment):
                available.add(for_match.group(1))
            if assign_match := _ASSIGN_RE.match(anchor_segment):
                available.add(assign_match.group(1))
    return violations


class TestBatchModeDesign:
    """Pins the per-edit snapshot/bisect mechanism — see plan blocker 5."""

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            # Bisect reverts via ``git apply -R`` on a per-edit patch file, not a file-scoped checkout.
            # ``quality-stack.md:106``'s ``git checkout HEAD -- <file>`` is file-scoped, the wrong tool for isolating
            # one edit's diff — a prior draft cited it as reusable precedent for per-edit revert, which the challenger
            # review found false (no command existed to reuse).
            pytest.param("git apply -R", 'git diff "$PRE_N" "$PRE_N_PLUS_1"', id="revert-uses-real-patch-apply"),
            # The doc states snapshots are dangling commits, unsafe across a ``git gc --prune``. Round-2 review (R4)
            # flagged this as undocumented; the snapshots are reachable only through sentinel files, never a ref or
            # branch, so a prune between snapshot and bisect would silently break the revert.
            pytest.param("dangling", "git gc --prune", id="snapshot-lifetime-documented-as-dangling"),
            # ``BATCH_ID``/``N`` are computed by a shipped command, never left as unassigned prose variables. A prior
            # draft named them in prose ("BATCH_ID = e.g. the batch's start timestamp") without a producing command —
            # both expanded empty at runtime, so every snapshot in a batch collided on one path.
            pytest.param(
                "BATCH_ID=$(date -u +%Y%m%dT%H%M%SZ)", "N=$((N + 1))", id="batch-id-and-n-derived-not-assumed"
            ),
            # Non-overlap holds on edited files AND test-impact sets, not test sets alone.
            pytest.param("shares no test", "touches no source file", id="non-overlap-covers-both-files-and-tests"),
            # Batch size is capped at 4 edits, bounding worst-case bisect cost.
            pytest.param("Cap: 4 edits per batch", "ceil(log2(4)) = 2", id="batch-cap-is-four-edits"),
            # Batch mode's own doc states it never applies to the review loops.
            pytest.param("Step 4 review loop", "Step 5's review loop", id="batch-scoped-to-named-loops-only"),
        ],
    )
    def test_batch_mode_doc_states_design_marker_pair(self, first: str, second: str) -> None:
        """``batch-mode.md`` still states both halves of each design fact its contract pins.

        The pairs cover the per-edit revert via ``git apply -R``, the dangling-snapshot lifetime, the derived
        ``BATCH_ID``/``N``, the non-overlap predicate, the 4-edit batch cap, and the scoping of batch mode away from the
        review loops. The rationale for each pair is in the comment above its case.
        """
        text = _text(_BATCH_MODE)
        assert first in text
        assert second in text

    def test_snapshot_uses_git_index_file_not_bare_diff(self) -> None:
        """The pre-edit snapshot is a real commit object built via a throwaway ``GIT_INDEX_FILE``.

        A plain ``git diff`` (no ``HEAD``) omits staged changes and drops untracked files entirely — a batch member
        adding a new test file would have no revert target at all. ``git stash create`` was tried first and rejected:
        empirically confirmed (live scratch repo) to error ``Entry '<file>' not up-to-date. Cannot merge.`` when
        combined with ``git add -N`` intent-to-add entries, and ``git stash create`` never captures untracked content at
        all. Redirecting every index op to a throwaway file (``read-tree`` / ``add -A`` / ``write-tree`` / ``commit-
        tree``) captures tracked and untracked content alike without ever touching the real index, branch, or stash
        list.
        """
        text = _text(_BATCH_MODE)
        assert "GIT_INDEX_FILE=" in text
        assert "git write-tree" in text
        assert 'git commit-tree "$_TREE" -p HEAD' in text
        assert "git diff > " not in text
        assert "git add -N ." not in text

    @pytest.mark.parametrize(
        ("present", "absent"),
        [
            # The snapshot's ``git add`` stages the whole repo root, never just the caller's cwd. Round-2 review (R4)
            # found ``git add -A .`` CWD-scoped: ``read-tree HEAD`` seeds the whole tree but ``add -A .`` only
            # re-stages the current directory down, so a skill run from a subdirectory would silently record files
            # outside it as unchanged-from-HEAD and omit their edits from every isolated patch. ``:/`` pathspec magic
            # matches the repo root regardless of cwd — verified live (scratch repo, snapshot taken from a
            # subdirectory, both a root-level and a sub-level file staged).
            pytest.param("git add -A :/", "git add -A .", id="snapshot-add-uses-repo-root-pathspec-not-cwd"),
            # The closing batch size is written to a sentinel the caller's cap-check fence reads back. ``BATCH_SIZE``
            # with no assignment anywhere always defaults to 1, silently capping the batch cap at edits-per-cycle
            # instead of edits-per-batch — this pins that the batch itself writes the value, off by one corrected
            # (``N`` counts every snapshot including the closing one, so edits processed is ``N - 1``). The sentinel
            # path is a real shell variable (``_SKILL``), never a literal ``<skill>`` placeholder — round-4 review
            # (N9) found the placeholder can never match the permission manifest and the caller's own reader side
            # already expects a real skill name (``dev-feature-batch-size-...``, ``dev-refactor-batch-size-...``).
            pytest.param(
                'echo "$((N - 1))" > "${TMPDIR:-/tmp}/dev-${_SKILL}-batch-size-${CSID}"',
                "dev-<skill>-batch-size",
                id="batch-size-persisted-for-cap-accounting",
            ),
            # The Cap Accounting prose states the base edit count only — not a phantom bisect-re-run addition. Round-4
            # review (N11) found the prose claimed a failing, bisecting batch "consumes 4 plus the bisect re-runs'
            # edits" while the shipped fence only ever writes the base edit count — no mechanism anywhere accumulates
            # the bisect re-runs on top of it. Documented behavior must match implemented behavior.
            pytest.param(
                "also consumes its base edit count",
                "plus the bisect re-runs' edits",
                id="cap-accounting-prose-matches-what-the-fence-writes",
            ),
        ],
    )
    def test_batch_mode_doc_states_marker_and_omits_stale_text(self, present: str, absent: str) -> None:
        """``batch-mode.md`` carries the current wording of a pinned design fact and none of its superseded form.

        Covers the repo-root pathspec of the snapshot ``git add``, the persisted batch size (a real ``_SKILL`` variable
        rather than a ``<skill>`` placeholder) and the cap-accounting prose. The rationale for each is in the comment
        above its case.
        """
        text = _text(_BATCH_MODE)
        assert present in text
        assert absent not in text

    def test_batch_run_reads_n_and_gates_sentinel_writes_on_success(self) -> None:
        """The batch-run write fence reads ``N`` back and only writes the size sentinel on success.

        An earlier draft used ``$N`` bare (never read back in this fence — a separate Bash() call from the one that
        derived it). Identity now clears in a separate fence (round-4, N9) — a fence containing ``rm -f`` is flagged
        ``is_dangerous`` and declined whole before any manifest lookup, so the write and the clear can no longer share
        one fence; the calling procedure invokes the clear fence only when ``BATCH_RUN_EXIT`` was 0.
        """
        text = _text(_BATCH_MODE)
        assert 'IFS= read -r N < "${TMPDIR:-/tmp}/dev-batch-n-${CSID}"' in text
        assert 'if [ "${BATCH_RUN_EXIT:-1}" -eq 0 ]; then' in text
        # the write fence and the clear fence are two separate ```bash blocks — the clear fence
        # must not contain the size-write, and must not itself contain an `if` gate (it relies on
        # the calling procedure to invoke it conditionally, documented in prose between the two)
        blocks = _bash_fences(text)
        write_fence = next(b for b in blocks if 'echo "$((N - 1))"' in b)
        clear_fence = next(
            b
            for b in blocks
            if b.strip().startswith(
                'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\nrm -f "${TMPDIR:-/tmp}/dev-batch-id'
            )
        )
        assert write_fence is not clear_fence
        assert "rm -f" not in write_fence
        assert 'echo "$((N - 1))"' not in clear_fence

    def test_snapshot_and_bisect_fences_read_back_batch_id(self) -> None:
        """Derivation, snapshot, bisect, and both post-bisect fences each re-read ``BATCH_ID`` from its sentinel.

        Shell state does not survive across Bash() calls — a fence consuming ``$BATCH_ID`` bare, derived only in an
        earlier fence, always expands empty, colliding every snapshot in a batch onto one path (``dev-batch-
        pre---${CSID}``). Five independent fences now read it back: derivation's own emptiness check, per-edit snapshot,
        bisect's patch loop, and the post-bisect size-write and identity-clear fences (split into two by round-4's N9
        fix, each needing its own ``BATCH_ID`` read-back).
        """
        text = _text(_BATCH_MODE)
        assert text.count('IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}"') == 5

    def test_bisect_n_is_a_real_loop_variable_not_prose(self) -> None:
        """The bisect fence assigns ``N`` via a shipped ``for`` loop — never left to "the calling procedure".

        Round-3 adversarial review (H3) found the prior draft named ``N`` in prose ("the calling procedure sets it per
        iteration") with no producing command — run verbatim, ``N`` expands empty, ``git diff "" "<sha>"`` exits 128,
        and no patch is written. The loop bound is derived from the persisted ``N`` total, never assumed.
        """
        text = _text(_BATCH_MODE)
        assert "IFS= read -r _N_TOTAL <" in text
        assert "_BATCH_EDITS=$((_N_TOTAL - 1))" in text
        assert 'for N in $(seq 1 "$_BATCH_EDITS"); do' in text
        assert "the calling procedure sets it per iteration" not in text

    def test_bisect_failure_path_resets_identity_and_persists_edit_count(self) -> None:
        """On a failed batch, identity clears and the real edit count is persisted — not left to the ``:-1`` default.

        Round-3 adversarial review (R6) found the success-only clear in Batch run left ``BATCH_ID``/``N`` dangling after
        every failed batch — the next batch's ``N`` kept accumulating, and the failed batch itself counted 1 against the
        cap instead of its real edit count. This pins the post-bisect write+clear fences that close both gaps. Round-4
        review (N9) split them into two fences (a ``rm -f`` fence is flagged ``is_dangerous`` regardless of what else
        shares it) and replaced the literal ``<skill>`` placeholder with a real ``_SKILL`` variable.
        """
        text = _text(_BATCH_MODE)
        assert 'echo "$_BATCH_EDITS" > "${TMPDIR:-/tmp}/dev-${_SKILL}-batch-size-${CSID}"' in text
        assert (
            'rm -f "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" "${TMPDIR:-/tmp}/dev-batch-n-${CSID}" '
            '"${TMPDIR:-/tmp}/dev-batch-edits-${BATCH_ID}-${CSID}"'
        ) in text
        # the write and the clear must come after the bisect loop, not before — bisect still needs
        # BATCH_ID/N in place — and must be two separate fences, not one
        loop_idx = text.index('for N in $(seq 1 "$_BATCH_EDITS"); do')
        write_idx = text.index('echo "$_BATCH_EDITS" > "${TMPDIR:-/tmp}/dev-${_SKILL}-batch-size-${CSID}"')
        assert loop_idx < write_idx
        blocks = _bash_fences(text)
        post_bisect_write = next(b for b in blocks if "$_BATCH_EDITS" in b and 'echo "$_BATCH_EDITS"' in b)
        post_bisect_clear = next(b for b in blocks if "dev-batch-edits-${BATCH_ID}-${CSID}" in b and "rm -f" in b)
        assert post_bisect_write is not post_bisect_clear

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param(_BATCH_MODE, id="batch-mode"),
            pytest.param(_FEATURE_SKILL, id="feature-skill"),
            pytest.param(_REFACTOR_SKILL, id="refactor-skill"),
        ],
    )
    def test_every_fence_variable_is_assigned_or_read_back_in_that_fence(self, path: Path) -> None:
        """No fence consumes a variable nothing assigns or reads back — the M9 derived-property test.

        Recommended in three consecutive adversarial rounds (W_1, W_2, W_3) and never written — the exact test that
        would have caught H3's bisect ``$N`` (round 3) and N9's ``<skill>`` placeholder (round 4) before a challenger
        had to find them by hand. Round 5 (F4) found the coverage scoped to ``batch-mode.md`` alone missed both skills
        that wire it in — extended here to ``feature/SKILL.md`` and ``refactor/SKILL.md``, which caught a real instance
        of the same class (Step 1's baseline-gate fence reading ``TDD_CYCLE``/``LAST_CYCLE_FULL_DIR`` from a separate,
        earlier fence — fixed by merging the read into the consuming fence). Runs over every fence in each file, not
        just the ones a past finding named.
        """
        text = _text(path)
        for i, fence in enumerate(_bash_fences(text)):
            violations = _fence_unassigned_var_refs(fence)
            assert not violations, f"{path.name} fence #{i} references unassigned var(s) {violations}:\n{fence}"

    @pytest.mark.parametrize(
        ("marker", "anchor", "later"),
        [
            # The bisect loop aborts on a missing pre-snapshot instead of diffing an empty path. Round-4 review (N10)
            # found both ``PRE_N``/``PRE_N_PLUS_1`` default to empty on a missing sentinel; ``git diff "" "<sha>"``
            # exits 128 but the redirect has already created an empty patch file, and ``git apply -R`` on an empty
            # patch succeeds as a silent no-op — bisect believes it reverted an edit it never touched.
            pytest.param(
                '[ -n "$PRE_N" ] && [ -n "$PRE_N_PLUS_1" ] || { echo',
                '[ -n "$PRE_N" ] && [ -n "$PRE_N_PLUS_1" ]',
                'git diff "$PRE_N" "$PRE_N_PLUS_1"',
                id="bisect-loop-guards-missing-snapshot-before-diffing",
            ),
            # ``_BATCH_EDITS`` cannot go negative — a missing ``batch-n`` sentinel aborts instead of underflowing.
            # Round-4 review (N12) found ``_BATCH_EDITS=$((_N_TOTAL - 1))`` with the sentinel absent (``_N_TOTAL``
            # defaults to 0) yields ``-1``, which ``seq 1 -1`` silently turns into zero patches and which step 7 would
            # then persist as a negative batch size, decrementing the caller's cap counter.
            pytest.param(
                '[ "$_N_TOTAL" -ge 1 ] || { echo',
                '[ "$_N_TOTAL" -ge 1 ]',
                "_BATCH_EDITS=$((_N_TOTAL - 1))",
                id="bisect-guards-missing-n-total-before-subtracting",
            ),
        ],
    )
    def test_bisect_guard_precedes_the_command_it_guards(self, marker: str, anchor: str, later: str) -> None:
        """A bisect-fence guard is present in ``batch-mode.md`` and comes before the command it protects.

        Covers the missing pre-snapshot guard ahead of ``git diff`` and the missing ``batch-n`` total guard ahead of the
        ``_BATCH_EDITS`` subtraction. The rationale for each is in the comment above its case.
        """
        text = _text(_BATCH_MODE)
        assert marker in text
        assert text.index(anchor) < text.index(later)


class TestBatchModeWiring:
    """Pins the opt-in flag wiring into feature Step 3 and refactor Step 4."""

    @pytest.mark.parametrize(
        ("path", "marker"),
        [
            # batch mode ships default-on, ``--no-batch`` opts out: BATCH_ENABLED defaults to true when the sentinel is
            # absent.
            pytest.param(
                _FEATURE_SKILL,
                '[ "$BATCH_ENABLED" = "false" ] || BATCH_ENABLED=true',
                id="feature-batch-defaults-true",
            ),
            pytest.param(
                _REFACTOR_SKILL,
                '[ "$BATCH_ENABLED" = "false" ] || BATCH_ENABLED=true',
                id="refactor-batch-defaults-true",
            ),
            # the cap-accounting increment defaults BATCH_SIZE to 1 — the no-batch path is unchanged.
            pytest.param(
                _FEATURE_SKILL,
                "TDD_CYCLE=$((TDD_CYCLE + ${BATCH_SIZE:-1}))",
                id="feature-cap-accounting-uses-batch-size",
            ),
            pytest.param(
                _REFACTOR_SKILL,
                "INNER_CYCLE=$((INNER_CYCLE + ${BATCH_SIZE:-1}))",
                id="refactor-cap-accounting-uses-batch-size",
            ),
            # MAX_INNER_CYCLES / INNER_CYCLE count edits processed, so the no-batch path is unaffected.
            pytest.param(
                _BATCH_MODE,
                "counts **edits processed**, not batches or cycles",
                id="cap-accounting-counts-edits-not-batches",
            ),
        ],
    )
    def test_cap_and_default_wiring_marker_present(self, path: Path, marker: str) -> None:
        """The feature/refactor skills and ``batch-mode.md`` still carry the opt-in default and cap-accounting wiring.

        BATCH_ENABLED defaults to true when its sentinel is absent, the cycle increment defaults ``BATCH_SIZE`` to 1 so
        the no-batch path is unchanged, and the cap counts edits processed rather than batches or cycles.
        """
        assert marker in _text(path)

    @pytest.mark.parametrize(
        ("path", "marker", "later"),
        [
            # feature/SKILL.md reads the real ``BATCH_SIZE`` batch-mode.md wrote, not just the ``:-1`` default.
            pytest.param(
                _FEATURE_SKILL,
                'IFS= read -r BATCH_SIZE < "${TMPDIR:-/tmp}/dev-feature-batch-size-${CSID}"',
                "TDD_CYCLE=$((TDD_CYCLE + ${BATCH_SIZE:-1}))",
                id="feature-reads-batch-size-sentinel-before-increment",
            ),
            # refactor/SKILL.md reads the real ``BATCH_SIZE`` batch-mode.md wrote, not just the ``:-1`` default.
            pytest.param(
                _REFACTOR_SKILL,
                'IFS= read -r BATCH_SIZE < "${TMPDIR:-/tmp}/dev-refactor-batch-size-${CSID}"',
                "INNER_CYCLE=$((INNER_CYCLE + ${BATCH_SIZE:-1}))",
                id="refactor-reads-batch-size-sentinel-before-increment",
            ),
            # Round-4 review (N9) found batch-mode.md's size-write fences used a literal ``<skill>`` placeholder — a
            # fenced block can never substitute a ``<placeholder>`` at runtime, and it can never match the permission
            # manifest either. The fix has each calling skill write its own name once, inside the same gate that
            # decides whether to load batch-mode.md at all — so a no-batch run never writes the sentinel.
            pytest.param(
                _FEATURE_SKILL,
                'echo "feature" > "${TMPDIR:-/tmp}/dev-batch-skill-${CSID}"',
                'cat "$_DEV_SHARED/batch-mode.md"',
                id="feature-writes-skill-name-sentinel-before-loading-batch-mode",
            ),
            # Same N9 fix as feature/SKILL.md, mirrored for refactor's own batch-mode gate.
            pytest.param(
                _REFACTOR_SKILL,
                'echo "refactor" > "${TMPDIR:-/tmp}/dev-batch-skill-${CSID}"',
                'cat "$_DEV_SHARED/batch-mode.md"',
                id="refactor-writes-skill-name-sentinel-before-loading-batch-mode",
            ),
        ],
    )
    def test_skill_orders_wiring_marker_before_later_marker(self, path: Path, marker: str, later: str) -> None:
        """A skill reads or writes its batch sentinel before the step that depends on it.

        The skill reads the real ``BATCH_SIZE`` batch-mode.md wrote before incrementing its cycle counter, and writes
        its own skill name to the sentinel batch-mode.md's fences read back before loading batch-mode.md.
        """
        text = _text(path)
        assert marker in text
        assert text.index(marker) < text.index(later)
