<!-- file: delegation.md — consumers: release/SKILL.md (Delegation strategy) -->

> **Agent waits** — SKILL.md §Agent wait discipline (batches `gather`, `changelog-audit`); never `ScheduleWakeup`, `ListAgents` or a `Monitor` loop.

In `prepare` and `audit` modes, delegate gather/explore/validate to subagent via file-based handoff (CLAUDE.md §2) — these phases produce large output, bloat main context:

1. Pre-compute gather file and waived-changes ledger paths, create dir:
   ```bash
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   # reload BRANCH/DATE written by SKILL.md Shared setup (Check 41)
   IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
   IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
   GATHER_FILE=".temp/release-gather-$BRANCH-$DATE.md"
   mkdir -p .temp  # timeout: 5000
   WAIVED_FILE=$(mktemp ".temp/release-waived-$BRANCH-$DATE-XXXXXXXX") || exit 1  # one atomic, distinct ledger per invocation; preserve earlier runs
   echo "$WAIVED_FILE" > "${TMPDIR:-/tmp}/release-waived-${CSID}"  # persist for downstream reload (Check 41) — see modes/classify-truth-check.md "Waived changes ledger"
   ```
2. Assert variables before spawning:
   ```bash
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   # reload every asserted var — fresh shell (Check 41)
   IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
   IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
   IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT=""
   IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
   IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
   IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
   IFS= read -r WAIVED_FILE < "${TMPDIR:-/tmp}/release-waived-${CSID}" 2>/dev/null || WAIVED_FILE=""
   GATHER_FILE=".temp/release-gather-$BRANCH-$DATE.md"
   [ -n "$GATHER_FILE" ] && [ -n "$WAIVED_FILE" ] && [ -n "$REPO_ROOT" ] && [ -n "$RANGE" ] || { echo "Error: GATHER_FILE, WAIVED_FILE, REPO_ROOT, or RANGE is empty — verify Shared setup and Gather changes completed"; exit 1; }  # timeout: 5000
   # LAST_TAG empty is valid (first-ever release, no prior tag) — gather-prompt.md's baseline check handles it, never asserted here
   OSS_BIN="${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin"  # resolved here, passed absolute: the gather subagent's shell has no CLAUDE_PLUGIN_ROOT
   echo "REPO_ROOT=$REPO_ROOT RANGE=$RANGE GATHER_FILE=$GATHER_FILE WAIVED_FILE=$WAIVED_FILE LAST_TAG=$LAST_TAG OSS_BIN=$OSS_BIN"  # literal values for the template substitution below
   cat "$SKILL_DIR/templates/gather-prompt.md"  # timeout: 5000
   ```

> **Agent budget** — spawn thresholds per `claude-config.md` §Agent/Skill Spawn Discipline. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

Template (loaded above). Substitute `<REPO_ROOT>`, `<RANGE>`, `<GATHER_FILE>`, `<WAIVED_FILE>`, `<LAST_TAG>`, `<OSS_BIN>` with literal values (`<LAST_TAG>` may substitute to an empty string — the template's own fallback covers that). Spawn:

> loads: gather-prompt.md `Agent(subagent_type="foundry:sw-engineer", name="gather-changes", description="Gather release range evidence", prompt=<substituted gather-prompt.md content>)`

3. Validate envelope; every "abort" is a hard `exit 1`. Bind `ENVELOPE` first: with the Write tool, save the gather agent's returned one-line JSON envelope verbatim to `${TMPDIR:-/tmp}/release-envelope-${CSID}`; the block reloads it. Validated counts persist to `release-envelope-unconfirmed*` sentinels, which the ledger reconcile block below reloads, so the two blocks need not share one Bash call:
   ```bash
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   [ -n "${ENVELOPE:-}" ] || IFS= read -r ENVELOPE < "${TMPDIR:-/tmp}/release-envelope-${CSID}" 2>/dev/null || ENVELOPE=""
   STATUS=$(echo "$ENVELOPE" | jq -r '.status' 2>/dev/null)
   GATHER_FILE=$(echo "$ENVELOPE" | jq -r '.file' 2>/dev/null)
   BREAKING=$(echo "$ENVELOPE" | jq -r '.breaking // 0' 2>/dev/null)  # default 0 — never skip migration guide on missing field
   UNCONFIRMED=$(echo "$ENVELOPE" | jq -er 'if (.unconfirmed | type) == "number" and (.unconfirmed >= 0) and (.unconfirmed == (.unconfirmed | floor)) then .unconfirmed else error("invalid unconfirmed") end' 2>/dev/null) || { echo "Error: missing or invalid unconfirmed count" >&2; exit 1; }
   UNCONFIRMED_BREAKING=$(echo "$ENVELOPE" | jq -er 'if (.unconfirmed_breaking | type) == "number" and (.unconfirmed_breaking >= 0) and (.unconfirmed_breaking == (.unconfirmed_breaking | floor)) then .unconfirmed_breaking else error("invalid unconfirmed_breaking") end' 2>/dev/null) || { echo "Error: missing or invalid unconfirmed_breaking count" >&2; exit 1; }
   if [ "$STATUS" != "done" ] || [ -z "$GATHER_FILE" ] || [ "$GATHER_FILE" = "null" ] || [ ! -f "$GATHER_FILE" ]; then
       echo "Error: delegation validation failed — status=$STATUS, file=$GATHER_FILE" >&2
       exit 1
   fi
   printf '%s\n' "$UNCONFIRMED" > "${TMPDIR:-/tmp}/release-envelope-unconfirmed-${CSID}"  # raw envelope count — never release-unconfirmed (verified-count signal, asserted absent on mismatch)
   printf '%s\n' "$UNCONFIRMED_BREAKING" > "${TMPDIR:-/tmp}/release-envelope-unconfirmed-breaking-${CSID}"
   ```

Validate the gather result against this invocation's ledger before any artifact phase. Each `unconfirmed` item has exactly one `REMOVED:` or `NET-STATE-ADD:` line; cross-cycle and post-merge entries use other prefixes. Persist the verified count for `prepare` Phase 6, which runs in a fresh shell:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -n "${UNCONFIRMED:-}" ] || IFS= read -r UNCONFIRMED < "${TMPDIR:-/tmp}/release-envelope-unconfirmed-${CSID}" 2>/dev/null || UNCONFIRMED=""
[ -n "${UNCONFIRMED_BREAKING:-}" ] || IFS= read -r UNCONFIRMED_BREAKING < "${TMPDIR:-/tmp}/release-envelope-unconfirmed-breaking-${CSID}" 2>/dev/null || UNCONFIRMED_BREAKING=""
IFS= read -r WAIVED_FILE < "${TMPDIR:-/tmp}/release-waived-${CSID}" 2>/dev/null || WAIVED_FILE=""
[ -n "$WAIVED_FILE" ] && [ -f "$WAIVED_FILE" ] || { echo "Error: waiver ledger missing after gather" >&2; exit 1; }
case "$UNCONFIRMED" in ''|*[!0-9]*) echo "Error: invalid unconfirmed count" >&2; exit 1;; esac
LEDGER_UNCONFIRMED=$(grep -Ec '^(REMOVED|NET-STATE-ADD):' "$WAIVED_FILE" || true)
[ "$LEDGER_UNCONFIRMED" -eq "$UNCONFIRMED" ] || { echo "Error: gather waiver count disagrees with ledger" >&2; exit 1; }
LEDGER_BREAKING=$(grep -Ec '^(REMOVED|NET-STATE-ADD): ⚠️ Breaking Changes:' "$WAIVED_FILE" || true)
[ "$LEDGER_BREAKING" -eq "$UNCONFIRMED_BREAKING" ] || { echo "Error: breaking waiver count disagrees with category ledger" >&2; exit 1; }
printf '%s\n' "$UNCONFIRMED" > "${TMPDIR:-/tmp}/release-unconfirmed-${CSID}"
```

> After the ledger reconcile above, apply SKILL.md's Delegation strategy gate (breaking-waiver review + unconfirmed notification), then continue with Phases 5–6 below (they run after phases 1–4 complete).

**Phases 5–6 parallel delegation** (`prepare`/`audit` modes, after phases 1–4 complete): Audit changelog and Extract contributors are independent — delegate concurrently to reclaim tokens.

Pre-compute output paths and persist for downstream reload (fresh shell, Check 41):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# fresh shell loses vars between blocks (Check 41)
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT=""
IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE="notes"
IFS= read -r VERSION < "${TMPDIR:-/tmp}/release-prepare-version-${CSID}" 2>/dev/null || VERSION=""
GATHER_FILE=".temp/release-gather-$BRANCH-$DATE.md"
[ -f "$GATHER_FILE" ] || { echo "Error: GATHER_FILE missing — phases 1–4 must complete first"; exit 1; }  # timeout: 5000
CHANGELOG_AUDIT_FILE=".temp/release-changelog-audit-$BRANCH-$DATE.md"
CONTRIBUTORS_FILE=".temp/release-contributors-$BRANCH-$DATE.md"
mkdir -p .temp  # timeout: 5000
echo "${CHANGELOG_AUDIT_FILE:-}" > "${TMPDIR:-/tmp}/release-changelog-audit-${CSID}"
echo "${CONTRIBUTORS_FILE:-}" > "${TMPDIR:-/tmp}/release-contributors-${CSID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/modes/changelog-audit-prompt.md"  # timeout: 5000
```

<!-- loads: modes/changelog-audit-prompt.md -->

Prompt (loaded above) — substitute `$RELEASE_MODE`, `$VERSION` along with the other listed variables, then execute (spawn Agent A + Agent B per instructions in that file).

Validate both envelopes: with the Write tool, save Agent A's and Agent B's returned one-line JSON envelopes verbatim to `${TMPDIR:-/tmp}/release-envelope-a-${CSID}` and `${TMPDIR:-/tmp}/release-envelope-b-${CSID}`; the block reloads them:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r ENVELOPE_A < "${TMPDIR:-/tmp}/release-envelope-a-${CSID}" 2>/dev/null || ENVELOPE_A=""
IFS= read -r ENVELOPE_B < "${TMPDIR:-/tmp}/release-envelope-b-${CSID}" 2>/dev/null || ENVELOPE_B=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse_release_envelopes.py" --envelope-a "$ENVELOPE_A" --envelope-b "$ENVELOPE_B"  # timeout: 5000
```

`$SCOPE_FLAGGED` > 0 → read the "Scope check" section of `$CHANGELOG_AUDIT_FILE`; every row there (critical = already-released commit landed a second time, medium = unreviewed branch merge) must reach the Findings summary table in `audit`/`prepare` modes — see `templates/audit-checks.md` Output, "Changelog scope" row — never left inside the audit file only. This is the check the reviewer needs surfaced before approving the PR, not something to bury in a report nobody opens. Same rule for the printed `unsourced` count: > 0 → copy each "unsourced entry" row from `$CHANGELOG_AUDIT_FILE` into the "Changelog evidence" row of the Findings summary (`templates/audit-checks.md`).
