<!-- oss:release adversarial review — executed via: cat "$SKILL_DIR/modes/adversarial-review.md"; execute -->

<!-- Variables available: $SKILL_DIR, $_OSS_SHARED, $BRANCH, $RANGE, $GATHER_FILE, assembled draft content -->

Challenge every factual claim in assembled draft against codebase, project docs. Runs before voice/tone polish — facts correct before prose refined.

**Scope**: applies to `notes` mode (DRAFT.md), `prepare` mode (releases/$VERSION/DRAFT.md), `--migration` flag output. Skip for `--summary` (internal audience), `--changelog` entries (structured format, not claim-heavy prose).

```bash
# expand to literal value before spawning
ADVERSARIAL_DIR=".temp/release-adversarial-$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')-$(date +%Y-%m-%d)"
mkdir -p "$ADVERSARIAL_DIR"  # timeout: 5000
```

Write full assembled draft content to `$ADVERSARIAL_DIR/draft-to-review.md` using Write tool.

Spawn adversarial reviewer — use `foundry:sw-engineer` for reliable tool execution (bash + read):

> **Agent waits** — SKILL.md §Agent wait discipline (batch `adversarial`); never `ScheduleWakeup`, `ListAgents` or a `Monitor` loop.

```text
Agent(subagent_type="foundry:sw-engineer", prompt="Adversarial review of a release draft against the project codebase and docs. Working directory: <REPO_ROOT>. Your job is to REFUTE claims, not confirm them — treat every stated fact as wrong until you prove it correct.

Read the release draft at: <$ADVERSARIAL_DIR/draft-to-review.md>

Challenge across 4 dimensions:

1. FACTUAL ACCURACY — for every named symbol in the draft ('adds X', 'fixes Y', 'removes Z'), verify the symbol is actually DEFINED in the codebase at HEAD — not just mentioned in a comment, docstring, or leftover stub. Prefer codemap over grep (codemap finds definitions; grep hits leftovers): check `codemap-py query find-symbol '^<symbol>$' 2>/dev/null` first; if codemap unavailable (`codemap-py query list` returns empty), fall back to `git grep -wl 'def <symbol>\|class <symbol>' HEAD -- '*.py' 2>/dev/null` (definition pattern) then `git grep -wl '<symbol>' HEAD -- '*.ts' '*.js' '*.go' '*.rs' 2>/dev/null`. Quote the output. Zero output for a claimed-added symbol = critical finding. Presence for a claimed-removed symbol = critical finding. Do not infer from GATHER_FILE — verify each symbol directly against HEAD.
2. COMPLETENESS — use GATHER_FILE (below) as the ground-truth commit list: significant commits (breaking changes, new public API) not mentioned in draft = high finding. Minor/internal commits absent is expected.
3. SEMVER CORRECTNESS — if draft says 'no breaking changes', run `git diff <RANGE> -- '*.py' '*.ts' '*.js' '*.go' '*.rs'` and scan for removed or renamed public API, changed function signatures, removed config keys. Flag contradictions.
4. DOCS ALIGNMENT — for each new API or behavior claimed in draft, confirm docs/ or README covers it. Missing doc = medium finding.

<gather context>
$([ -n \"<GATHER_FILE>\" ] && [ -f \"<GATHER_FILE>\" ] && echo 'Commit classification (for dimension 2 COMPLETENESS only): <GATHER_FILE> — read it to find commits the draft should cover. Do NOT use this file to verify factual accuracy — use HEAD git grep for dimension 1.' || echo 'Use the git range <RANGE> directly: git log <RANGE> --no-merges --oneline to get the change list.')
</gather context>

For each finding: severity (critical=claim directly contradicts HEAD state | high=significant missing change or SemVer misclassification | medium=overstated claim or minor missing item | low=style/wording inaccuracy). For critical and high findings: quote exact draft text AND quote git grep output or codebase evidence.

Write full findings report to <$ADVERSARIAL_DIR/adversarial-review.md> using the Write tool.
Return ONLY on your final line: {\"status\":\"done\",\"file\":\"<$ADVERSARIAL_DIR/adversarial-review.md>\",\"critical\":N,\"high\":N,\"medium\":N,\"low\":N,\"confidence\":0.N}")
```

Expand `<REPO_ROOT>`, `<RANGE>`, `<GATHER_FILE>`, `<$ADVERSARIAL_DIR>` to literal values before spawning — never pass variable names literally.

**Pre-handover truth check loop** — no output reaches user until all critical and high findings resolved:

1. Read `$ADVERSARIAL_DIR/adversarial-review.md` from spawned reviewer
2. **Critical or high findings present**: fix every claim in draft contradicted by HEAD (remove unverified API names, correct wrong descriptions, remove symbols absent from codebase); re-spawn adversarial reviewer on updated draft; repeat until zero critical/high findings or 3 total iterations reached
3. **After max 3 iterations with persistent critical/high findings**: surface remaining findings to user, stop — don't hand over draft with known false claims
4. **Medium or low findings**: append as `> ⚠ Reviewer notes: <summary>` to response; these don't block handover
5. **0 critical/high findings**: proceed to Polish

Log each fix: `[CORRECTED] <original claim> → <what changed and why>`.
